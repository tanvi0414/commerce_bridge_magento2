# -*- coding: utf-8 -*-
import hashlib
import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .commerce_instance import json_dumps, json_loads

_logger = logging.getLogger(__name__)


class CommerceBridgeBindingMixin(models.AbstractModel):
    _name = "commerce.bridge.binding.mixin"
    _description = "CommerceBridge Binding Mixin"

    instance_id = fields.Many2one("commerce.bridge.instance", required=True, ondelete="cascade", index=True)
    website_id = fields.Many2one("commerce.bridge.website", ondelete="set null")
    store_view_id = fields.Many2one("commerce.bridge.store.view", ondelete="set null")
    magento_id = fields.Char(index=True)
    magento_increment_id = fields.Char(index=True)
    external_ref = fields.Char(index=True)
    sync_state = fields.Selection([
        ("new", "New"),
        ("synced", "Synced"),
        ("needs_review", "Needs Review"),
        ("error", "Error"),
    ], default="new", index=True)
    last_sync_hash = fields.Char(index=True)
    last_import_at = fields.Datetime()
    last_export_at = fields.Datetime()
    last_error = fields.Text()
    active_on_magento = fields.Boolean(default=True)
    raw_json = fields.Text(readonly=True)

    def _payload_hash(self, payload):
        return hashlib.sha256(json.dumps(payload or {}, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class CommerceBridgeCategoryBinding(models.Model):
    _name = "commerce.bridge.category.binding"
    _description = "Magento Category Binding"
    _inherit = "commerce.bridge.binding.mixin"
    _order = "instance_id, magento_id"

    name = fields.Char(required=True)
    magento_parent_id = fields.Char()
    odoo_category_id = fields.Many2one("product.category", ondelete="set null")
    path = fields.Char()
    level = fields.Integer()
    position = fields.Integer()

    _sql_constraints = [
        ("uniq_category", "unique(instance_id, magento_id)", "This Magento category is already mapped."),
    ]


class CommerceBridgeProductBinding(models.Model):
    _name = "commerce.bridge.product.binding"
    _description = "Magento Product Binding"
    _inherit = "commerce.bridge.binding.mixin"
    _order = "instance_id, sku"

    name = fields.Char(required=True)
    sku = fields.Char(required=True, index=True)
    type_id = fields.Selection([
        ("simple", "Simple"),
        ("configurable", "Configurable"),
        ("virtual", "Virtual"),
        ("downloadable", "Downloadable"),
        ("bundle", "Bundle"),
        ("grouped", "Grouped"),
        ("other", "Other"),
    ], default="simple")
    attribute_set_id = fields.Char()
    odoo_product_id = fields.Many2one("product.product", ondelete="set null")
    odoo_template_id = fields.Many2one("product.template", related="odoo_product_id.product_tmpl_id", store=True, readonly=True)
    category_binding_ids = fields.Many2many(
        "commerce.bridge.category.binding",
        relation="cb_product_category_rel",
        column1="product_binding_id",
        column2="category_binding_id",
        string="Magento Categories",
    )    
    magento_status = fields.Integer()
    price = fields.Float()
    weight = fields.Float()
    visibility = fields.Integer()
    magento_updated_at = fields.Char()
    parent_sku = fields.Char(index=True)
    child_skus = fields.Text(help="JSON list of child SKUs for configurable products.")
    custom_attributes_json = fields.Text()

    _sql_constraints = [
        ("uniq_product_sku", "unique(instance_id, sku)", "This Magento SKU is already mapped."),
    ]

    def action_open_product(self):
        self.ensure_one()
        if not self.odoo_product_id:
            raise UserError(_("No Odoo product is linked."))
        return {
            "type": "ir.actions.act_window",
            "name": self.odoo_product_id.display_name,
            "res_model": "product.product",
            "view_mode": "form",
            "res_id": self.odoo_product_id.id,
        }

    def action_enqueue_stock_export(self):
        for rec in self:
            if not rec.odoo_product_id:
                raise UserError(_("Map %s to an Odoo product before exporting stock.") % rec.sku)
            rec.instance_id._create_job(
                operation="export_stock",
                name=_("Export stock %s") % rec.sku,
                model_name="product.product",
                res_id=rec.odoo_product_id.id,
                magento_id=rec.magento_id,
                payload={"sku": rec.sku},
                priority=40,
            )
        return True


class CommerceBridgePartnerBinding(models.Model):
    _name = "commerce.bridge.partner.binding"
    _description = "Magento Customer Binding"
    _inherit = "commerce.bridge.binding.mixin"
    _order = "instance_id, email"

    partner_id = fields.Many2one("res.partner", ondelete="set null")
    email = fields.Char(index=True)
    firstname = fields.Char()
    lastname = fields.Char()
    group_id = fields.Char()
    website_magento_id = fields.Char()
    address_fingerprint = fields.Char(index=True)

    _sql_constraints = [
        ("uniq_partner_email_instance", "unique(instance_id, email, website_magento_id)", "This Magento customer email is already mapped for this website."),
    ]


class CommerceBridgeSaleOrderBinding(models.Model):
    _name = "commerce.bridge.sale.order.binding"
    _description = "Magento Sale Order Binding"
    _inherit = "commerce.bridge.binding.mixin"
    _order = "magento_created_at desc, id desc"

    sale_id = fields.Many2one("sale.order", ondelete="set null")
    partner_id = fields.Many2one("res.partner", related="sale_id.partner_id", store=True, readonly=True)
    magento_status = fields.Char(index=True)
    magento_state = fields.Char(index=True)
    magento_created_at = fields.Char(index=True)
    magento_updated_at = fields.Char(index=True)
    currency_code = fields.Char()
    grand_total = fields.Float()
    subtotal = fields.Float()
    shipping_amount = fields.Float()
    discount_amount = fields.Float()
    tax_amount = fields.Float()
    payment_method = fields.Char()
    shipping_method = fields.Char()
    order_items_json = fields.Text(help="Magento item_id/SKU/qty mapping for shipment export.")
    total_difference = fields.Float()

    _sql_constraints = [
        ("uniq_order", "unique(instance_id, magento_id)", "This Magento order is already imported."),
        ("uniq_order_increment", "unique(instance_id, magento_increment_id)", "This Magento order number is already imported."),
    ]

    def action_open_order(self):
        self.ensure_one()
        if not self.sale_id:
            raise UserError(_("No Odoo sale order is linked."))
        return {
            "type": "ir.actions.act_window",
            "name": self.sale_id.display_name,
            "res_model": "sale.order",
            "view_mode": "form",
            "res_id": self.sale_id.id,
        }


class ProductProduct(models.Model):
    _inherit = "product.product"

    commerce_bridge_magento_binding_ids = fields.One2many("commerce.bridge.product.binding", "odoo_product_id", string="Magento Bindings")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    commerce_bridge_magento_binding_ids = fields.One2many("commerce.bridge.sale.order.binding", "sale_id", string="Magento Bindings")
