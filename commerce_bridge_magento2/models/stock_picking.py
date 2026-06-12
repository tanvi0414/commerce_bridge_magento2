# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class StockPicking(models.Model):
    _inherit = "stock.picking"

    commerce_bridge_magento_exported = fields.Boolean(string="Magento Shipment Exported", copy=False)
    commerce_bridge_magento_last_export_at = fields.Datetime(copy=False)

    def action_commerce_bridge_export_magento_shipment(self):
        for picking in self:
            if picking.picking_type_code != "outgoing":
                raise UserError(_("Only delivery orders can be exported as Magento shipments."))
            if picking.state != "done":
                raise UserError(_("Validate the delivery order before exporting it to Magento."))
            if not picking.sale_id:
                raise UserError(_("This delivery order is not linked to a sale order."))
            binding = self.env["commerce.bridge.sale.order.binding"].search([("sale_id", "=", picking.sale_id.id)], limit=1)
            if not binding:
                raise UserError(_("This sale order was not imported from Magento."))
            job = binding.instance_id._create_job(
                operation="export_shipment",
                name=_("Export Magento shipment for %s") % picking.name,
                model_name="stock.picking",
                res_id=picking.id,
                magento_id=binding.magento_id,
                payload={"picking_id": picking.id},
                priority=15,
            )
            picking.message_post(body=_("Magento shipment export job created: %s") % job.name)
        return True
