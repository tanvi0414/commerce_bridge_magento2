# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class CommerceBridgeInventoryDrift(models.Model):
    _name = "commerce.bridge.inventory.drift"
    _description = "CommerceBridge Inventory Drift"
    _order = "state desc, abs_difference desc, sku"

    instance_id = fields.Many2one("commerce.bridge.instance", required=True, ondelete="cascade", index=True)
    product_id = fields.Many2one("product.product", ondelete="set null")
    product_binding_id = fields.Many2one("commerce.bridge.product.binding", ondelete="cascade")
    sku = fields.Char(required=True, index=True)
    odoo_qty = fields.Float()
    magento_qty = fields.Float()
    difference = fields.Float()
    abs_difference = fields.Float()
    magento_source_code = fields.Char()
    state = fields.Selection([
        ("ok", "OK"),
        ("warning", "Warning"),
        ("critical", "Critical"),
        ("not_found", "Not Found in Magento"),
    ], default="ok", index=True)
    checked_at = fields.Datetime(default=fields.Datetime.now)
    raw_json = fields.Text()

    _sql_constraints = [
        ("uniq_drift", "unique(instance_id, sku, magento_source_code)", "Inventory drift already exists for this SKU/source."),
    ]

    @api.model
    def run_for_instances(self, instances):
        for instance in instances:
            client = instance._client()
            bindings = self.env["commerce.bridge.product.binding"].search([
                ("instance_id", "=", instance.id),
                ("odoo_product_id", "!=", False),
                ("sku", "!=", False),
            ], limit=500)
            for binding in bindings:
                sku = binding.sku
                product = binding.odoo_product_id
                odoo_qty = getattr(product, instance.stock_quantity_field, 0.0) or 0.0
                odoo_qty = max(0.0, float(odoo_qty) - float(instance.safety_stock or 0.0))
                magento_qty = 0.0
                state = "not_found"
                raw = []
                try:
                    if instance.stock_api_mode == "msi":
                        raw = client.get_source_items([sku], source_code=instance.stock_source_code or "default")
                        if raw:
                            magento_qty = float(raw[0].get("quantity") or 0.0)
                            state = "ok"
                    else:
                        product_data = client.get_product(sku)
                        stock = ((product_data.get("extension_attributes") or {}).get("stock_item") or {}) if isinstance(product_data, dict) else {}
                        magento_qty = float(stock.get("qty") or 0.0)
                        state = "ok"
                except Exception as exc:
                    raw = [{"error": str(exc)}]
                    state = "not_found"
                diff = round(magento_qty - odoo_qty, 4)
                abs_diff = abs(diff)
                if state == "ok" and abs_diff > 10:
                    state = "critical"
                elif state == "ok" and abs_diff > 0.0001:
                    state = "warning"
                vals = {
                    "instance_id": instance.id,
                    "product_id": product.id,
                    "product_binding_id": binding.id,
                    "sku": sku,
                    "odoo_qty": odoo_qty,
                    "magento_qty": magento_qty,
                    "difference": diff,
                    "abs_difference": abs_diff,
                    "magento_source_code": instance.stock_source_code or "default",
                    "state": state,
                    "checked_at": fields.Datetime.now(),
                    "raw_json": str(raw),
                }
                existing = self.search([("instance_id", "=", instance.id), ("sku", "=", sku), ("magento_source_code", "=", vals["magento_source_code"])], limit=1)
                existing.write(vals) if existing else self.create(vals)
        return True

    def action_export_odoo_stock(self):
        for rec in self:
            if not rec.product_id:
                raise UserError(_("No Odoo product is linked to this drift row."))
            rec.instance_id._create_job(
                operation="export_stock",
                name=_("Fix stock drift %s") % rec.sku,
                model_name="product.product",
                res_id=rec.product_id.id,
                payload={"sku": rec.sku},
                priority=5,
            )
        return True
