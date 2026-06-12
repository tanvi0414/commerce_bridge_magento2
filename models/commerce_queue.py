# -*- coding: utf-8 -*-
import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .commerce_instance import json_dumps, json_loads
from .magento_client import MagentoApiError

_logger = logging.getLogger(__name__)


class CommerceBridgeQueueJob(models.Model):
    _name = "commerce.bridge.queue.job"
    _description = "CommerceBridge Sync Job"
    _inherit = ["mail.thread"]
    _order = "priority, create_date, id"

    name = fields.Char(required=True)
    instance_id = fields.Many2one("commerce.bridge.instance", required=True, ondelete="cascade", index=True)
    operation = fields.Selection([
        ("import_categories", "Import Categories"),
        ("import_products", "Import Products"),
        ("import_orders", "Import Orders"),
        ("export_stock", "Export Stock"),
        ("export_shipment", "Export Shipment"),
        ("export_price", "Export Price"),
        ("reconcile_inventory", "Reconcile Inventory"),
        ("custom", "Custom"),
    ], required=True, index=True)
    state = fields.Selection([
        ("pending", "Pending"),
        ("running", "Running"),
        ("done", "Done"),
        ("failed", "Failed"),
        ("retry_waiting", "Retry Waiting"),
        ("blocked", "Blocked"),
        ("cancelled", "Cancelled"),
    ], default="pending", tracking=True, index=True)
    priority = fields.Integer(default=10, index=True)
    model_name = fields.Char(index=True)
    res_id = fields.Integer(index=True)
    magento_id = fields.Char(index=True)
    external_key = fields.Char(index=True)
    retry_count = fields.Integer(default=0)
    max_retries = fields.Integer(default=5)
    next_retry_at = fields.Datetime(index=True)
    started_at = fields.Datetime()
    finished_at = fields.Datetime()
    duration_ms = fields.Integer()
    payload_json = fields.Text()
    response_json = fields.Text()
    error_type = fields.Char()
    error_message = fields.Text()
    help_message = fields.Text()
    log_ids = fields.One2many("commerce.bridge.sync.log", "job_id")

    _sql_constraints = [
        ("uniq_open_job", "unique(instance_id, operation, model_name, res_id, magento_id, external_key, state)", "Duplicate open sync job."),
    ]

    @api.model
    def create_or_merge(self, instance, operation, name=None, model_name=None, res_id=None, magento_id=None, payload=None, priority=10):
        external_key = self._make_external_key(operation, model_name, res_id, magento_id, payload or {})
        existing = self.search([
            ("instance_id", "=", instance.id),
            ("operation", "=", operation),
            ("external_key", "=", external_key),
            ("state", "in", ["pending", "retry_waiting", "failed", "blocked"]),
        ], limit=1)
        vals = {
            "name": name or operation,
            "instance_id": instance.id,
            "operation": operation,
            "model_name": model_name,
            "res_id": res_id or 0,
            "magento_id": magento_id,
            "external_key": external_key,
            "payload_json": json_dumps(payload or {}),
            "priority": priority,
            "state": "pending",
            "error_message": False,
            "error_type": False,
            "help_message": False,
        }
        if existing:
            existing.write(vals)
            return existing
        return self.create(vals)

    @api.model
    def _make_external_key(self, operation, model_name=None, res_id=None, magento_id=None, payload=None):
        payload = payload or {}
        if payload.get("sku"):
            return "%s:%s" % (operation, payload.get("sku"))
        if payload.get("increment_id"):
            return "%s:%s" % (operation, payload.get("increment_id"))
        if model_name and res_id:
            return "%s:%s:%s" % (operation, model_name, res_id)
        if magento_id:
            return "%s:magento:%s" % (operation, magento_id)
        return "%s:global" % operation

    def action_retry(self):
        self.write({
            "state": "pending",
            "retry_count": 0,
            "next_retry_at": False,
            "error_type": False,
            "error_message": False,
            "help_message": False,
        })

    def action_cancel(self):
        self.write({"state": "cancelled"})

    def action_process(self):
        for job in self:
            job._process_one()
        return True

    @api.model
    def _cron_process_queue(self):
        return self._process_batch(limit=100)

    @api.model
    def _process_batch(self, instance_ids=None, limit=50):
        now = fields.Datetime.now()
        domain = [
            ("state", "in", ["pending", "retry_waiting"]),
            "|", ("next_retry_at", "=", False), ("next_retry_at", "<=", now),
        ]
        if instance_ids:
            domain.append(("instance_id", "in", instance_ids))
        jobs = self.search(domain, order="priority, create_date, id", limit=limit)
        for job in jobs:
            try:
                job._process_one()
                self.env.cr.commit()
            except Exception:
                _logger.exception("CommerceBridge queue job crashed: %s", job.display_name)
                self.env.cr.rollback()
        return True

    def _process_one(self):
        self.ensure_one()
        if self.state not in ("pending", "retry_waiting", "failed", "blocked"):
            return False
        start = fields.Datetime.now()
        self.write({"state": "running", "started_at": start, "error_message": False, "help_message": False})
        try:
            method = getattr(self, "_job_%s" % self.operation, None)
            if not method:
                raise UserError(_("Unsupported job operation: %s") % self.operation)
            result = method(json_loads(self.payload_json, {}))
            end = fields.Datetime.now()
            self.write({
                "state": "done",
                "finished_at": end,
                "duration_ms": int((end - start).total_seconds() * 1000),
                "response_json": json_dumps(result or {"ok": True}),
            })
            return True
        except Exception as exc:
            self._handle_failure(exc, start)
            return False

    def _handle_failure(self, exc, start):
        end = fields.Datetime.now()
        retry_count = self.retry_count + 1
        recoverable = not isinstance(exc, UserError) or isinstance(exc, MagentoApiError)
        max_retries = self.max_retries or 0
        if recoverable and retry_count <= max_retries:
            delay_minutes = min(60, 2 ** max(0, retry_count - 1))
            state = "retry_waiting"
            next_retry_at = fields.Datetime.now() + timedelta(minutes=delay_minutes)
        else:
            state = "failed" if recoverable else "blocked"
            next_retry_at = False
        msg = str(exc)
        self.write({
            "state": state,
            "retry_count": retry_count,
            "next_retry_at": next_retry_at,
            "finished_at": end,
            "duration_ms": int((end - start).total_seconds() * 1000),
            "error_type": exc.__class__.__name__,
            "error_message": msg,
            "help_message": self._explain_error(msg),
        })
        self._log("error", msg)

    def _explain_error(self, message):
        text = (message or "").lower()
        if "no such entity" in text and "sku" in text:
            return _("Magento could not find the SKU. Check product mapping, product visibility, and whether the SKU exists in Magento.")
        if "attribute" in text and "does not exist" in text:
            return _("Magento rejected a product attribute or option. Import attributes, map them in Mapping Center, then retry.")
        if "consumer" in text or "message queue" in text:
            return _("Magento accepted the request but its queue may not be running. Check Magento consumers and retry later.")
        if "unauthorized" in text or "401" in text:
            return _("Magento token is invalid or lacks permissions. Recreate the integration token with required ACL permissions.")
        if "timeout" in text:
            return _("Magento took too long to respond. Reduce batch size or increase timeout on the connector instance.")
        if "tax" in text:
            return _("Tax mapping/configuration may be incomplete. Check taxes and order total policy in Mapping Center.")
        return _("Open the payload/response, fix the mapped data, then use Retry. This job is safe to retry.")

    def _log(self, level, message, endpoint=None, request_payload=None, response_payload=None, duration_ms=0):
        self.env["commerce.bridge.sync.log"].create({
            "job_id": self.id,
            "instance_id": self.instance_id.id,
            "level": level,
            "message": message,
            "operation": self.operation,
            "model_name": self.model_name,
            "res_id": self.res_id,
            "magento_id": self.magento_id,
            "endpoint": endpoint,
            "request_json": json_dumps(request_payload or {}),
            "response_json": json_dumps(response_payload or {}),
            "duration_ms": duration_ms,
        })

    # Job implementations
    def _job_import_categories(self, payload):
        instance = self.instance_id
        client = instance._client()
        imported = 0
        for item in client.iter_categories(page_size=int(payload.get("page_size") or instance.page_size or 100)):
            self._upsert_category(item)
            imported += 1
        return {"imported_or_updated": imported}

    def _upsert_category(self, item):
        instance = self.instance_id
        Binding = self.env["commerce.bridge.category.binding"]
        Category = self.env["product.category"]
        magento_id = str(item.get("id") or item.get("entity_id") or "")
        if not magento_id:
            return False
        name = item.get("name") or ("Magento Category %s" % magento_id)
        binding = Binding.search([("instance_id", "=", instance.id), ("magento_id", "=", magento_id)], limit=1)
        odoo_category = binding.odoo_category_id if binding else Category.browse()
        if not odoo_category:
            odoo_category = Category.search([("name", "=", name)], limit=1)
        if not odoo_category:
            odoo_category = Category.create({"name": name})
        vals = {
            "instance_id": instance.id,
            "magento_id": magento_id,
            "magento_parent_id": str(item.get("parent_id")) if item.get("parent_id") is not None else False,
            "name": name,
            "odoo_category_id": odoo_category.id,
            "path": item.get("path"),
            "level": int(item.get("level") or 0),
            "position": int(item.get("position") or 0),
            "active_on_magento": bool(item.get("is_active", True)),
            "sync_state": "synced",
            "last_import_at": fields.Datetime.now(),
            "raw_json": json_dumps(item),
        }
        binding.write(vals) if binding else Binding.create(vals)
        return True

    def _job_import_products(self, payload):
        instance = self.instance_id
        client = instance._client()
        # Keep category mapping fresh before product import so category_ids are useful.
        try:
            self._job_import_categories(payload)
        except Exception as exc:
            self._log("warning", _("Category import skipped during product import: %s") % exc)
        imported = 0
        updated_from = payload.get("updated_from")
        page_size = int(payload.get("page_size") or instance.page_size or 100)
        for item in client.iter_products(updated_from=updated_from, page_size=page_size):
            self._upsert_product(item)
            imported += 1
            if imported % 100 == 0:
                self.env.cr.commit()
        instance.write({"last_product_import_at": fields.Datetime.now()})
        return {"imported_or_updated": imported}

    def _upsert_product(self, item):
        instance = self.instance_id
        Product = self.env["product.product"].with_context(active_test=False)
        Template = self.env["product.template"].with_context(active_test=False)
        Binding = self.env["commerce.bridge.product.binding"]
        sku = item.get("sku")
        if not sku:
            return False
        name = item.get("name") or sku
        type_id = item.get("type_id") or "other"
        price = float(item.get("price") or 0.0)
        product = Product.search([("default_code", "=", sku)], limit=1)
        if not product and instance.auto_create_products:
            vals = {
                "name": name,
                "default_code": sku,
                "list_price": price,
                "sale_ok": True,
                "purchase_ok": True,
            }
            if "detailed_type" in Template._fields:
                vals["detailed_type"] = "product" if type_id in ("simple", "configurable", "bundle", "grouped") else "consu"
            elif "type" in Template._fields:
                vals["type"] = "product" if type_id in ("simple", "configurable", "bundle", "grouped") else "consu"
            tmpl = Template.create(vals)
            product = tmpl.product_variant_id
        custom_attributes = item.get("custom_attributes") or []
        category_bindings = self._resolve_product_categories(custom_attributes)
        vals = {
            "instance_id": instance.id,
            "magento_id": str(item.get("id")) if item.get("id") is not None else False,
            "external_ref": sku,
            "sku": sku,
            "name": name,
            "type_id": type_id if type_id in dict(Binding._fields["type_id"].selection) else "other",
            "attribute_set_id": str(item.get("attribute_set_id")) if item.get("attribute_set_id") is not None else False,
            "odoo_product_id": product.id if product else False,
            "magento_status": int(item.get("status") or 0),
            "price": price,
            "weight": float(item.get("weight") or 0.0),
            "visibility": int(item.get("visibility") or 0),
            "magento_updated_at": item.get("updated_at"),
            "custom_attributes_json": json_dumps(custom_attributes),
            "raw_json": json_dumps(item),
            "sync_state": "synced" if product else "needs_review",
            "last_import_at": fields.Datetime.now(),
        }
        binding = Binding.search([("instance_id", "=", instance.id), ("sku", "=", sku)], limit=1)
        if binding:
            binding.write(vals)
        else:
            binding = Binding.create(vals)
        if category_bindings:
            binding.category_binding_ids = [(6, 0, category_bindings.ids)]
        if product:
            updates = {}
            if product.name != name:
                updates["name"] = name
            current_price = getattr(product, "lst_price", getattr(product, "list_price", 0.0))
            if price and current_price != price:
                if "lst_price" in product._fields:
                    updates["lst_price"] = price
                elif "list_price" in product._fields:
                    updates["list_price"] = price
                else:
                    product.product_tmpl_id.with_context(skip_commerce_bridge=True).write({"list_price": price})
            if updates:
                product.with_context(skip_commerce_bridge=True).write(updates)
        return binding

    def _resolve_product_categories(self, custom_attributes):
        category_ids = []
        for attr in custom_attributes or []:
            if attr.get("attribute_code") == "category_ids":
                value = attr.get("value") or []
                if isinstance(value, str):
                    value = [v.strip() for v in value.split(",") if v.strip()]
                category_ids = [str(v) for v in value]
                break
        if not category_ids:
            return self.env["commerce.bridge.category.binding"]
        return self.env["commerce.bridge.category.binding"].search([
            ("instance_id", "=", self.instance_id.id),
            ("magento_id", "in", category_ids),
        ])

    def _job_import_orders(self, payload):
        instance = self.instance_id
        client = instance._client()
        imported = 0
        created_from = payload.get("created_from")
        page_size = int(payload.get("page_size") or instance.page_size or 100)
        for item in client.iter_orders(created_from=created_from, page_size=page_size):
            self._upsert_order(item)
            imported += 1
            if imported % 50 == 0:
                self.env.cr.commit()
        instance.write({"last_order_import_at": fields.Datetime.now()})
        return {"imported_or_updated": imported}

    def _upsert_order(self, item):
        instance = self.instance_id
        Binding = self.env["commerce.bridge.sale.order.binding"]
        magento_id = str(item.get("entity_id") or item.get("order_id") or "")
        increment_id = str(item.get("increment_id") or magento_id)
        if not magento_id:
            return False
        binding = Binding.search(["|", ("magento_id", "=", magento_id), ("magento_increment_id", "=", increment_id), ("instance_id", "=", instance.id)], limit=1)
        if binding and binding.sale_id:
            binding.write({
                "magento_status": item.get("status"),
                "magento_state": item.get("state"),
                "magento_updated_at": item.get("updated_at"),
                "raw_json": json_dumps(item),
                "last_import_at": fields.Datetime.now(),
            })
            return binding
        partner = self._find_or_create_partner(item)
        order_lines = self._prepare_order_lines(item)
        if not order_lines:
            raise UserError(_("Magento order %s has no importable lines.") % increment_id)
        sale_vals = {
            "partner_id": partner.id,
            "origin": "Magento %s" % increment_id,
            "client_order_ref": increment_id,
            "company_id": instance.company_id.id,
            "order_line": [(0, 0, line) for line in order_lines],
        }
        if item.get("created_at"):
            sale_vals["date_order"] = item.get("created_at")
        sale = self.env["sale.order"].with_context(mail_create_nosubscribe=True).create(sale_vals)
        diff = self._apply_order_total_policy(sale, item)
        if instance.default_order_action == "confirm":
            sale.action_confirm()
        order_items = []
        for line in item.get("items") or []:
            if line.get("parent_item_id"):
                continue
            order_items.append({
                "item_id": line.get("item_id"),
                "sku": line.get("sku"),
                "qty_ordered": line.get("qty_ordered"),
                "qty_shipped": line.get("qty_shipped"),
            })
        vals = {
            "instance_id": instance.id,
            "magento_id": magento_id,
            "magento_increment_id": increment_id,
            "sale_id": sale.id,
            "magento_status": item.get("status"),
            "magento_state": item.get("state"),
            "magento_created_at": item.get("created_at"),
            "magento_updated_at": item.get("updated_at"),
            "currency_code": item.get("order_currency_code") or item.get("base_currency_code"),
            "grand_total": float(item.get("grand_total") or 0.0),
            "subtotal": float(item.get("subtotal") or 0.0),
            "shipping_amount": float(item.get("shipping_amount") or 0.0),
            "discount_amount": float(item.get("discount_amount") or 0.0),
            "tax_amount": float(item.get("tax_amount") or 0.0),
            "payment_method": ((item.get("payment") or {}).get("method")),
            "shipping_method": item.get("shipping_method"),
            "order_items_json": json_dumps(order_items),
            "total_difference": diff,
            "raw_json": json_dumps(item),
            "sync_state": "synced",
            "last_import_at": fields.Datetime.now(),
        }
        binding = Binding.create(vals)
        sale.message_post(body=_("Imported from Magento order %s.") % increment_id)
        return binding

    def _find_or_create_partner(self, order):
        Partner = self.env["res.partner"]
        email = order.get("customer_email") or ((order.get("billing_address") or {}).get("email"))
        firstname = order.get("customer_firstname") or (order.get("billing_address") or {}).get("firstname") or "Magento"
        lastname = order.get("customer_lastname") or (order.get("billing_address") or {}).get("lastname") or "Customer"
        partner = email and Partner.search([("email", "=", email)], limit=1) or Partner.browse()
        if not partner:
            billing = order.get("billing_address") or {}
            partner = Partner.create({
                "name": ("%s %s" % (firstname, lastname)).strip(),
                "email": email,
                "phone": billing.get("telephone"),
                "street": " ".join([p for p in [billing.get("street") and ", ".join(billing.get("street")) if isinstance(billing.get("street"), list) else billing.get("street")] if p]),
                "city": billing.get("city"),
                "zip": billing.get("postcode"),
                "customer_rank": 1,
            })
        self._upsert_partner_binding(partner, order)
        return partner

    def _upsert_partner_binding(self, partner, order):
        email = order.get("customer_email") or ((order.get("billing_address") or {}).get("email"))
        if not email:
            return False
        Binding = self.env["commerce.bridge.partner.binding"]
        magento_customer_id = order.get("customer_id")
        website_id = order.get("store_id")
        vals = {
            "instance_id": self.instance_id.id,
            "partner_id": partner.id,
            "magento_id": str(magento_customer_id) if magento_customer_id else False,
            "email": email,
            "firstname": order.get("customer_firstname"),
            "lastname": order.get("customer_lastname"),
            "website_magento_id": str(website_id) if website_id is not None else False,
            "sync_state": "synced",
            "last_import_at": fields.Datetime.now(),
        }
        binding = Binding.search([
            ("instance_id", "=", self.instance_id.id),
            ("email", "=", email),
            ("website_magento_id", "=", vals["website_magento_id"]),
        ], limit=1)
        binding.write(vals) if binding else Binding.create(vals)

    def _prepare_order_lines(self, order):
        Product = self.env["product.product"].with_context(active_test=False)
        Template = self.env["product.template"].with_context(active_test=False)
        lines = []
        for item in order.get("items") or []:
            # Magento configurable child lines often have parent_item_id; import parent sale line only.
            if item.get("parent_item_id"):
                continue
            sku = item.get("sku")
            name = item.get("name") or sku or "Magento item"
            product = sku and Product.search([("default_code", "=", sku)], limit=1) or Product.browse()
            if not product:
                if not self.instance_id.auto_create_products:
                    raise UserError(_("Product with SKU %s is not mapped and auto-create is disabled.") % sku)
                vals = {"name": name, "default_code": sku or False, "sale_ok": True, "purchase_ok": True}
                if "detailed_type" in Template._fields:
                    vals["detailed_type"] = "product"
                elif "type" in Template._fields:
                    vals["type"] = "product"
                product = Template.create(vals).product_variant_id
            qty = float(item.get("qty_ordered") or item.get("qty") or 0.0)
            if qty <= 0:
                continue
            row_total = float(item.get("row_total") or 0.0)
            discount = abs(float(item.get("discount_amount") or 0.0))
            price_unit = row_total / qty if qty else float(item.get("price") or 0.0)
            discount_pct = (discount / row_total * 100.0) if row_total else 0.0
            lines.append({
                "product_id": product.id,
                "name": name,
                "product_uom_qty": qty,
                "price_unit": price_unit,
                "discount": min(100.0, discount_pct),
            })
        shipping_amount = float(order.get("shipping_amount") or 0.0)
        if shipping_amount:
            shipping_product = self.env.ref("commerce_bridge_magento2.product_magento_shipping", raise_if_not_found=False)
            if shipping_product:
                lines.append({
                    "product_id": shipping_product.product_variant_id.id,
                    "name": order.get("shipping_description") or _("Magento Shipping"),
                    "product_uom_qty": 1.0,
                    "price_unit": shipping_amount,
                })
        return lines

    def _apply_order_total_policy(self, sale, order):
        expected = float(order.get("grand_total") or 0.0)
        actual = sale.amount_total
        diff = round(expected - actual, 2)
        if abs(diff) <= self.instance_id.order_total_tolerance:
            return diff
        if self.instance_id.order_total_policy == "block":
            sale.unlink()
            raise UserError(_("Magento order total differs from Odoo by %.2f. Import blocked by guardrail.") % diff)
        if self.instance_id.order_total_policy == "adjust":
            adjustment_product = self.env.ref("commerce_bridge_magento2.product_magento_adjustment", raise_if_not_found=False)
            if adjustment_product:
                self.env["sale.order.line"].create({
                    "order_id": sale.id,
                    "product_id": adjustment_product.product_variant_id.id,
                    "name": _("Magento total adjustment"),
                    "product_uom_qty": 1.0,
                    "price_unit": diff,
                })
        if self.instance_id.order_total_policy == "warn":
            sale.message_post(body=_("Magento/Odoo total difference: %.2f") % diff)
        return diff

    def _job_export_stock(self, payload):
        instance = self.instance_id
        sku = payload.get("sku")
        product = self.env["product.product"].browse(self.res_id).exists() if self.model_name == "product.product" and self.res_id else False
        if not product and sku:
            binding = self.env["commerce.bridge.product.binding"].search([("instance_id", "=", instance.id), ("sku", "=", sku)], limit=1)
            product = binding.odoo_product_id
        if not product:
            raise UserError(_("No Odoo product found for stock export."))
        sku = sku or product.default_code
        if not sku:
            raise UserError(_("Product %s has no SKU/default code.") % product.display_name)
        qty = getattr(product, instance.stock_quantity_field, 0.0) or 0.0
        qty = max(0.0, float(qty) - float(instance.safety_stock or 0.0))
        client = instance._client()
        if instance.stock_api_mode == "msi":
            source_item = {
                "sku": sku,
                "source_code": instance.stock_source_code or "default",
                "quantity": qty,
                "status": 1 if qty > 0 else 0,
            }
            result = client.save_source_items([source_item])
        else:
            result = client.update_classic_stock_item(sku, qty, qty > 0)
        instance.write({"last_stock_export_at": fields.Datetime.now()})
        binding = self.env["commerce.bridge.product.binding"].search([("instance_id", "=", instance.id), ("sku", "=", sku)], limit=1)
        if binding:
            binding.write({"last_export_at": fields.Datetime.now(), "sync_state": "synced"})
        return {"sku": sku, "quantity": qty, "result": result}

    def _job_export_shipment(self, payload):
        instance = self.instance_id
        picking = self.env["stock.picking"].browse(payload.get("picking_id") or self.res_id).exists()
        if not picking:
            raise UserError(_("No Odoo delivery order found for shipment export."))
        sale = picking.sale_id
        binding = self.env["commerce.bridge.sale.order.binding"].search([("instance_id", "=", instance.id), ("sale_id", "=", sale.id)], limit=1)
        if not binding:
            raise UserError(_("This delivery order is not linked to a Magento order."))
        order_items = json_loads(binding.order_items_json, [])
        sku_to_item = {str(row.get("sku")): row for row in order_items if row.get("sku")}
        items = []
        for move in picking.move_ids_without_package:
            sku = move.product_id.default_code
            if not sku or sku not in sku_to_item:
                continue
            qty_done = getattr(move, "quantity", False) or getattr(move, "quantity_done", False) or move.product_uom_qty
            if qty_done:
                items.append({"order_item_id": sku_to_item[sku].get("item_id"), "qty": float(qty_done)})
        if not items:
            raise UserError(_("No Magento order items could be matched to this delivery."))
        tracks = []
        tracking = getattr(picking, "carrier_tracking_ref", False)
        if tracking:
            tracks.append({
                "track_number": tracking,
                "title": picking.carrier_id.name if getattr(picking, "carrier_id", False) else "Carrier",
                "carrier_code": "custom",
            })
        result = instance._client().create_shipment(binding.magento_id, items=items, tracks=tracks, notify=True, comment="Shipped from Odoo")
        self._log("info", _("Shipment exported to Magento."), response_payload=result)
        return {"shipment_id": result, "items": items}


class CommerceBridgeSyncLog(models.Model):
    _name = "commerce.bridge.sync.log"
    _description = "CommerceBridge Sync Log"
    _order = "create_date desc, id desc"

    job_id = fields.Many2one("commerce.bridge.queue.job", ondelete="cascade")
    instance_id = fields.Many2one("commerce.bridge.instance", required=True, ondelete="cascade", index=True)
    level = fields.Selection([("debug", "Debug"), ("info", "Info"), ("warning", "Warning"), ("error", "Error")], default="info", index=True)
    message = fields.Text(required=True)
    operation = fields.Char(index=True)
    model_name = fields.Char(index=True)
    res_id = fields.Integer(index=True)
    magento_id = fields.Char(index=True)
    endpoint = fields.Char()
    duration_ms = fields.Integer()
    request_json = fields.Text()
    response_json = fields.Text()
