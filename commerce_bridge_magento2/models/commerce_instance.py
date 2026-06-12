# -*- coding: utf-8 -*-
import json
import logging
from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT

from .magento_client import MagentoClient, MagentoApiError

_logger = logging.getLogger(__name__)


def json_dumps(value):
    return json.dumps(value or {}, ensure_ascii=False, default=str, indent=2)


def json_loads(value, default=None):
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except Exception:
        return default if default is not None else {}


class CommerceBridgeInstance(models.Model):
    _name = "commerce.bridge.instance"
    _description = "CommerceBridge Magento Instance"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "sequence, name"

    name = fields.Char(required=True, tracking=True, default="Magento 2")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, required=True)
    base_url = fields.Char(required=True, help="Example: https://shop.example.com", tracking=True)
    access_token = fields.Char(required=True, groups="commerce_bridge_magento2.group_commerce_bridge_manager", help="Magento Admin/Integration access token.")
    store_code = fields.Char(default="all", help="Use all unless you intentionally want a specific Magento store code.")
    verify_ssl = fields.Boolean(default=True)
    timeout = fields.Integer(default=60)
    enabled = fields.Boolean(default=True, tracking=True)
    notes = fields.Text()

    # Workflow controls
    import_order_from = fields.Datetime(default=lambda self: fields.Datetime.now() - timedelta(days=30), tracking=True)
    import_product_from = fields.Datetime(help="Optional product updated_at lower bound.")
    page_size = fields.Integer(default=100)
    auto_create_products = fields.Boolean(default=True, help="Create placeholder Odoo products when Magento order items are not mapped.")
    default_order_action = fields.Selection([
        ("draft", "Create quotation only"),
        ("confirm", "Confirm sale order"),
    ], default="draft", required=True)
    order_total_policy = fields.Selection([
        ("block", "Block when totals differ"),
        ("adjust", "Add adjustment line"),
        ("warn", "Import with warning"),
    ], default="adjust", required=True)
    order_total_tolerance = fields.Float(default=0.05)

    stock_api_mode = fields.Selection([
        ("msi", "Magento MSI source items"),
        ("classic", "Classic stock item"),
    ], default="msi", required=True)
    stock_source_code = fields.Char(default="default", help="Magento MSI source_code. Use 'default' for single-source stores.")
    warehouse_id = fields.Many2one("stock.warehouse", string="Default Odoo Warehouse")
    stock_quantity_field = fields.Selection([
        ("free_qty", "Available / Free to Use"),
        ("qty_available", "On Hand"),
        ("virtual_available", "Forecasted"),
    ], default="free_qty", required=True)
    safety_stock = fields.Float(default=0.0, help="Quantity deducted before sending stock to Magento.")
    export_stock_only_bound_products = fields.Boolean(default=True)

    # State / metrics
    connection_state = fields.Selection([
        ("not_tested", "Not Tested"),
        ("ok", "Connected"),
        ("error", "Connection Error"),
    ], default="not_tested", tracking=True)
    last_connection_at = fields.Datetime(readonly=True)
    last_connection_error = fields.Text(readonly=True)
    magento_version = fields.Char(readonly=True)
    readiness_score = fields.Integer(default=0, readonly=True, tracking=True)
    last_readiness_at = fields.Datetime(readonly=True)
    last_product_import_at = fields.Datetime(readonly=True)
    last_order_import_at = fields.Datetime(readonly=True)
    last_stock_export_at = fields.Datetime(readonly=True)

    website_ids = fields.One2many("commerce.bridge.website", "instance_id")
    store_ids = fields.One2many("commerce.bridge.store", "instance_id")
    store_view_ids = fields.One2many("commerce.bridge.store.view", "instance_id")
    mapping_ids = fields.One2many("commerce.bridge.mapping", "instance_id")
    readiness_check_ids = fields.One2many("commerce.bridge.readiness.check", "instance_id")

    product_binding_count = fields.Integer(compute="_compute_counts")
    order_binding_count = fields.Integer(compute="_compute_counts")
    failed_job_count = fields.Integer(compute="_compute_counts")
    pending_job_count = fields.Integer(compute="_compute_counts")
    drift_count = fields.Integer(compute="_compute_counts")

    _sql_constraints = [
        ("base_url_unique_company", "unique(base_url, company_id)", "This Magento URL is already configured for this company."),
    ]

    @api.constrains("base_url")
    def _check_base_url(self):
        for rec in self:
            if rec.base_url and not rec.base_url.lower().startswith(("http://", "https://")):
                raise ValidationError(_("Magento URL must start with http:// or https://"))

    @api.depends()
    def _compute_counts(self):
        ProductBinding = self.env["commerce.bridge.product.binding"]
        OrderBinding = self.env["commerce.bridge.sale.order.binding"]
        Job = self.env["commerce.bridge.queue.job"]
        Drift = self.env["commerce.bridge.inventory.drift"]
        for rec in self:
            rec.product_binding_count = ProductBinding.search_count([("instance_id", "=", rec.id)])
            rec.order_binding_count = OrderBinding.search_count([("instance_id", "=", rec.id)])
            rec.failed_job_count = Job.search_count([("instance_id", "=", rec.id), ("state", "in", ["failed", "blocked"])])
            rec.pending_job_count = Job.search_count([("instance_id", "=", rec.id), ("state", "in", ["pending", "retry_waiting"])])
            rec.drift_count = Drift.search_count([("instance_id", "=", rec.id), ("state", "!=", "ok")])

    def _client(self):
        self.ensure_one()
        return MagentoClient(
            self.base_url,
            self.access_token,
            timeout=self.timeout,
            verify_ssl=self.verify_ssl,
            store_code=self.store_code or "all",
        )

    def _dt_to_magento(self, value):
        if not value:
            return None
        if isinstance(value, str):
            return value
        return fields.Datetime.to_string(value)

    def _create_job(self, operation, name=None, model_name=None, res_id=None, magento_id=None, payload=None, priority=10):
        self.ensure_one()
        return self.env["commerce.bridge.queue.job"].create_or_merge(
            instance=self,
            operation=operation,
            name=name or operation.replace("_", " ").title(),
            model_name=model_name,
            res_id=res_id,
            magento_id=magento_id,
            payload=payload or {},
            priority=priority,
        )

    def action_test_connection(self):
        for rec in self:
            try:
                client = rec._client()
                websites = client.get_websites()
                modules = []
                try:
                    modules = client.get_modules()
                except Exception:
                    modules = []
                version = ""
                if isinstance(modules, list):
                    # Some stores expose module list but not product version. Keep a useful diagnostic.
                    version = "Modules: %s" % len(modules)
                rec.write({
                    "connection_state": "ok",
                    "last_connection_at": fields.Datetime.now(),
                    "last_connection_error": False,
                    "magento_version": version or "Connected",
                })
                rec.message_post(body=_("Magento connection successful. Websites found: %s") % len(websites or []))
            except Exception as exc:
                rec.write({
                    "connection_state": "error",
                    "last_connection_at": fields.Datetime.now(),
                    "last_connection_error": str(exc),
                })
                raise UserError(_("Magento connection failed:\n%s") % exc)
        return True

    def action_import_stores(self):
        Website = self.env["commerce.bridge.website"]
        Store = self.env["commerce.bridge.store"]
        StoreView = self.env["commerce.bridge.store.view"]
        for rec in self:
            client = rec._client()
            for item in client.get_websites() or []:
                vals = {
                    "instance_id": rec.id,
                    "magento_id": str(item.get("id")),
                    "code": item.get("code"),
                    "name": item.get("name") or item.get("code"),
                    "is_default": bool(item.get("is_default")),
                    "raw_json": json_dumps(item),
                }
                existing = Website.search([("instance_id", "=", rec.id), ("magento_id", "=", vals["magento_id"])], limit=1)
                existing.write(vals) if existing else Website.create(vals)
            for item in client.get_store_groups() or []:
                vals = {
                    "instance_id": rec.id,
                    "magento_id": str(item.get("id")),
                    "website_magento_id": str(item.get("website_id")) if item.get("website_id") is not None else False,
                    "code": item.get("code") or str(item.get("id")),
                    "name": item.get("name") or item.get("code") or str(item.get("id")),
                    "root_category_id": str(item.get("root_category_id")) if item.get("root_category_id") is not None else False,
                    "raw_json": json_dumps(item),
                }
                existing = Store.search([("instance_id", "=", rec.id), ("magento_id", "=", vals["magento_id"])], limit=1)
                existing.write(vals) if existing else Store.create(vals)
            for item in client.get_store_views() or []:
                vals = {
                    "instance_id": rec.id,
                    "magento_id": str(item.get("id")),
                    "store_magento_id": str(item.get("store_group_id")) if item.get("store_group_id") is not None else False,
                    "code": item.get("code"),
                    "name": item.get("name") or item.get("code"),
                    "is_active": bool(item.get("is_active", True)),
                    "raw_json": json_dumps(item),
                }
                existing = StoreView.search([("instance_id", "=", rec.id), ("magento_id", "=", vals["magento_id"])], limit=1)
                existing.write(vals) if existing else StoreView.create(vals)
            rec.message_post(body=_("Magento websites/stores/store views imported."))
        return True

    def action_enqueue_product_import(self):
        for rec in self:
            rec._create_job(
                operation="import_products",
                name=_("Import Magento products"),
                payload={"updated_from": rec._dt_to_magento(rec.import_product_from), "page_size": rec.page_size},
                priority=20,
            )
        return self.action_open_jobs()

    def action_enqueue_order_import(self):
        for rec in self:
            rec._create_job(
                operation="import_orders",
                name=_("Import Magento orders"),
                payload={"created_from": rec._dt_to_magento(rec.import_order_from), "page_size": rec.page_size},
                priority=30,
            )
        return self.action_open_jobs()

    def action_enqueue_stock_export(self):
        ProductBinding = self.env["commerce.bridge.product.binding"]
        for rec in self:
            domain = [("instance_id", "=", rec.id), ("odoo_product_id", "!=", False), ("sku", "!=", False)]
            bindings = ProductBinding.search(domain)
            if not bindings and not rec.export_stock_only_bound_products:
                # Fallback: export every active product with SKU.
                products = self.env["product.product"].search([("default_code", "!=", False), ("active", "=", True)])
                for product in products:
                    rec._create_job(
                        operation="export_stock",
                        name=_("Export stock %s") % product.display_name,
                        model_name="product.product",
                        res_id=product.id,
                        payload={"sku": product.default_code},
                        priority=40,
                    )
            for binding in bindings:
                rec._create_job(
                    operation="export_stock",
                    name=_("Export stock %s") % binding.sku,
                    model_name="product.product",
                    res_id=binding.odoo_product_id.id,
                    magento_id=binding.magento_id,
                    payload={"sku": binding.sku},
                    priority=40,
                )
        return self.action_open_jobs()

    def action_run_readiness(self):
        self.env["commerce.bridge.readiness.check"].run_for_instances(self)
        return self.action_open_readiness()

    def action_reconcile_inventory(self):
        self.env["commerce.bridge.inventory.drift"].run_for_instances(self)
        return self.action_open_drift()

    def action_process_queue(self):
        self.env["commerce.bridge.queue.job"]._process_batch(instance_ids=self.ids, limit=50)
        return self.action_open_jobs()

    def action_open_jobs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Sync Jobs"),
            "res_model": "commerce.bridge.queue.job",
            "view_mode": "list,form",
            "domain": [("instance_id", "=", self.id)],
            "context": {"default_instance_id": self.id},
        }

    def action_open_failed_jobs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Failed Jobs"),
            "res_model": "commerce.bridge.queue.job",
            "view_mode": "list,form",
            "domain": [("instance_id", "=", self.id), ("state", "in", ["failed", "blocked"])],
            "context": {"default_instance_id": self.id},
        }

    def action_open_products(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Magento Products"),
            "res_model": "commerce.bridge.product.binding",
            "view_mode": "list,form",
            "domain": [("instance_id", "=", self.id)],
            "context": {"default_instance_id": self.id},
        }

    def action_open_orders(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Magento Orders"),
            "res_model": "commerce.bridge.sale.order.binding",
            "view_mode": "list,form",
            "domain": [("instance_id", "=", self.id)],
            "context": {"default_instance_id": self.id},
        }

    def action_open_readiness(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Readiness Checks"),
            "res_model": "commerce.bridge.readiness.check",
            "view_mode": "list,form",
            "domain": [("instance_id", "=", self.id)],
            "context": {"default_instance_id": self.id},
        }

    def action_open_drift(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Inventory Drift"),
            "res_model": "commerce.bridge.inventory.drift",
            "view_mode": "list,form",
            "domain": [("instance_id", "=", self.id)],
            "context": {"default_instance_id": self.id},
        }


class CommerceBridgeWebsite(models.Model):
    _name = "commerce.bridge.website"
    _description = "Magento Website"
    _order = "instance_id, magento_id"

    instance_id = fields.Many2one("commerce.bridge.instance", required=True, ondelete="cascade")
    magento_id = fields.Char(required=True)
    code = fields.Char()
    name = fields.Char(required=True)
    is_default = fields.Boolean()
    raw_json = fields.Text(readonly=True)

    _sql_constraints = [
        ("uniq_website", "unique(instance_id, magento_id)", "Website already exists for this instance."),
    ]


class CommerceBridgeStore(models.Model):
    _name = "commerce.bridge.store"
    _description = "Magento Store Group"
    _order = "instance_id, magento_id"

    instance_id = fields.Many2one("commerce.bridge.instance", required=True, ondelete="cascade")
    magento_id = fields.Char(required=True)
    website_magento_id = fields.Char()
    code = fields.Char()
    name = fields.Char(required=True)
    root_category_id = fields.Char()
    raw_json = fields.Text(readonly=True)

    _sql_constraints = [
        ("uniq_store", "unique(instance_id, magento_id)", "Store already exists for this instance."),
    ]


class CommerceBridgeStoreView(models.Model):
    _name = "commerce.bridge.store.view"
    _description = "Magento Store View"
    _order = "instance_id, magento_id"

    instance_id = fields.Many2one("commerce.bridge.instance", required=True, ondelete="cascade")
    magento_id = fields.Char(required=True)
    store_magento_id = fields.Char()
    code = fields.Char()
    name = fields.Char(required=True)
    is_active = fields.Boolean(default=True)
    raw_json = fields.Text(readonly=True)

    _sql_constraints = [
        ("uniq_store_view", "unique(instance_id, magento_id)", "Store view already exists for this instance."),
    ]
