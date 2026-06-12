# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class CommerceBridgeMapping(models.Model):
    _name = "commerce.bridge.mapping"
    _description = "CommerceBridge Mapping Center"
    _inherit = ["mail.thread"]
    _order = "instance_id, mapping_type, magento_value"

    instance_id = fields.Many2one("commerce.bridge.instance", required=True, ondelete="cascade")
    mapping_type = fields.Selection([
        ("payment_method", "Payment Method"),
        ("shipping_method", "Shipping Method"),
        ("tax_class", "Tax Class"),
        ("order_status", "Order Status"),
        ("customer_group", "Customer Group"),
        ("warehouse_source", "Warehouse / Source"),
        ("attribute", "Product Attribute"),
        ("attribute_option", "Attribute Option"),
        ("price_list", "Pricelist / Website"),
        ("b2b_company", "B2B Company"),
        ("shared_catalog", "Shared Catalog"),
        ("custom", "Custom"),
    ], required=True, tracking=True)
    magento_value = fields.Char(required=True, tracking=True)
    magento_label = fields.Char()
    odoo_model = fields.Char(help="Technical model name for the mapped Odoo record, when applicable.")
    odoo_res_id = fields.Integer(help="Database ID of the mapped Odoo record, when applicable.")
    odoo_display_name = fields.Char(compute="_compute_odoo_display_name", store=False)
    odoo_value = fields.Char(help="Text fallback when the mapping is not tied to a single Odoo record.")
    required_for_sync = fields.Boolean(default=True)
    active = fields.Boolean(default=True)
    state = fields.Selection([
        ("unmapped", "Unmapped"),
        ("mapped", "Mapped"),
        ("ignored", "Ignored"),
    ], default="unmapped", tracking=True)
    notes = fields.Text()

    _sql_constraints = [
        ("uniq_mapping", "unique(instance_id, mapping_type, magento_value)", "This Magento value is already mapped for this type."),
    ]

    def _compute_odoo_display_name(self):
        for rec in self:
            rec.odoo_display_name = rec._get_odoo_display_name()

    def _get_odoo_display_name(self):
        self.ensure_one()
        if self.odoo_model and self.odoo_res_id and self.odoo_model in self.env:
            target = self.env[self.odoo_model].browse(self.odoo_res_id).exists()
            if target:
                return target.display_name
        return self.odoo_value or ""

    def action_mark_ignored(self):
        self.write({"state": "ignored", "required_for_sync": False})

    def action_mark_unmapped(self):
        self.write({"state": "unmapped", "required_for_sync": True})

    def action_mark_mapped(self):
        for rec in self:
            if not rec.odoo_value and not (rec.odoo_model and rec.odoo_res_id):
                raise UserError(_("Add an Odoo value/record before marking this mapping as mapped."))
        self.write({"state": "mapped"})

    @classmethod
    def find_value(cls, env, instance, mapping_type, magento_value, default=None):
        mapping = env["commerce.bridge.mapping"].search([
            ("instance_id", "=", instance.id),
            ("mapping_type", "=", mapping_type),
            ("magento_value", "=", str(magento_value)),
            ("state", "=", "mapped"),
        ], limit=1)
        if not mapping:
            return default
        if mapping.odoo_value:
            return mapping.odoo_value
        if mapping.odoo_model and mapping.odoo_res_id and mapping.odoo_model in env:
            return env[mapping.odoo_model].browse(mapping.odoo_res_id).exists() or default
        return default
