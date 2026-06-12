# -*- coding: utf-8 -*-
from collections import Counter

from odoo import _, api, fields, models


class CommerceBridgeReadinessCheck(models.Model):
    _name = "commerce.bridge.readiness.check"
    _description = "CommerceBridge Readiness Check"
    _order = "severity desc, category, name"

    instance_id = fields.Many2one("commerce.bridge.instance", required=True, ondelete="cascade", index=True)
    category = fields.Selection([
        ("connection", "Connection"),
        ("catalog", "Catalog"),
        ("orders", "Orders"),
        ("inventory", "Inventory"),
        ("mapping", "Mapping"),
        ("security", "Security"),
        ("performance", "Performance"),
        ("b2b", "B2B / Enterprise"),
    ], required=True, index=True)
    severity = fields.Selection([
        ("ok", "OK"),
        ("info", "Info"),
        ("warning", "Warning"),
        ("blocking", "Blocking"),
    ], default="ok", required=True, index=True)
    name = fields.Char(required=True)
    details = fields.Text()
    action_hint = fields.Text()
    affected_count = fields.Integer(default=0)
    state = fields.Selection([
        ("open", "Open"),
        ("resolved", "Resolved"),
        ("ignored", "Ignored"),
    ], default="open", index=True)

    @api.model
    def run_for_instances(self, instances):
        for instance in instances:
            self.search([("instance_id", "=", instance.id)]).unlink()
            checks = []
            def add(category, severity, name, details="", action_hint="", count=0):
                checks.append({
                    "instance_id": instance.id,
                    "category": category,
                    "severity": severity,
                    "name": name,
                    "details": details,
                    "action_hint": action_hint,
                    "affected_count": count,
                })

            # Connection
            try:
                instance._client().get_websites()
                add("connection", "ok", _("Magento API connection works"), _("The connector can read Magento websites."))
            except Exception as exc:
                add("connection", "blocking", _("Magento API connection failed"), str(exc), _("Fix URL/token/permissions, then click Test Connection."))

            # Configuration
            if not instance.warehouse_id:
                add("inventory", "warning", _("Default Odoo warehouse is not selected"), _("Stock export can still run, but warehouse/source mapping will be less clear."), _("Select a warehouse on the connector instance."))
            if instance.stock_api_mode == "msi" and not instance.stock_source_code:
                add("inventory", "blocking", _("Magento source code is missing"), _("MSI stock export requires source_code."), _("Set source_code, usually 'default' for a single-source Magento store."))
            if not instance.import_order_from:
                add("orders", "warning", _("Order import cut-off date is empty"), _("A first import may try to pull old Magento order history."), _("Set a safe historical import date before creating import jobs."))

            # Store metadata
            if not instance.store_view_ids:
                add("connection", "warning", _("Magento store views have not been imported"), _("Store views are required for multi-store visibility and pricing rules."), _("Click Import Stores."))

            # Mapping status
            required_unmapped = self.env["commerce.bridge.mapping"].search_count([
                ("instance_id", "=", instance.id),
                ("required_for_sync", "=", True),
                ("state", "=", "unmapped"),
            ])
            if required_unmapped:
                add("mapping", "blocking", _("Required mappings are incomplete"), _("%s required mappings are still unmapped.") % required_unmapped, _("Open Mapping Center and map or ignore them."), required_unmapped)
            else:
                add("mapping", "ok", _("No blocking unmapped required mappings"))

            # SKU quality
            products_with_sku = self.env["product.product"].search([("default_code", "!=", False), ("active", "=", True)])
            sku_counter = Counter([p.default_code for p in products_with_sku if p.default_code])
            duplicates = [sku for sku, count in sku_counter.items() if count > 1]
            if duplicates:
                add("catalog", "blocking", _("Duplicate Odoo SKUs found"), _("Duplicate SKUs break safe product/stock sync. Examples: %s") % ", ".join(duplicates[:10]), _("Make default_code unique before exporting stock."), len(duplicates))
            else:
                add("catalog", "ok", _("Odoo SKUs are unique"))

            missing_sku = self.env["product.product"].search_count([("default_code", "=", False), ("active", "=", True), ("sale_ok", "=", True)])
            if missing_sku:
                add("catalog", "warning", _("Saleable Odoo products without SKU"), _("%s saleable products have no Internal Reference/SKU.") % missing_sku, _("Add SKUs before stock/price export."), missing_sku)

            failed_jobs = self.env["commerce.bridge.queue.job"].search_count([("instance_id", "=", instance.id), ("state", "in", ["failed", "blocked"])])
            if failed_jobs:
                add("performance", "warning", _("Failed or blocked jobs exist"), _("%s jobs need attention.") % failed_jobs, _("Open Sync Jobs, fix the issue, then retry."), failed_jobs)

            if instance.stock_api_mode == "msi":
                add("inventory", "info", _("MSI mode enabled"), _("Stock export will use Magento inventory source items."))
            else:
                add("inventory", "info", _("Classic stock mode enabled"), _("Stock export will update product stock item ID 1."))

            add("security", "info", _("Token is stored in Odoo and hidden from normal users"), _("Only connector managers can see/edit the access token."), _("Use a Magento Integration token with least required permissions."))
            add("b2b", "info", _("B2B shared catalog features are scaffolded"), _("Enterprise B2B endpoints require Adobe Commerce B2B modules and customer-specific mapping."), _("Use Mapping Center to prepare companies/customer groups before adding live B2B sync."))

            if checks:
                self.create(checks)
            penalty = 0
            for c in checks:
                if c["severity"] == "blocking":
                    penalty += 25
                elif c["severity"] == "warning":
                    penalty += 10
                elif c["severity"] == "info":
                    penalty += 2
            score = max(0, min(100, 100 - penalty))
            instance.write({"readiness_score": score, "last_readiness_at": fields.Datetime.now()})
        return True

    def action_ignore(self):
        self.write({"state": "ignored"})

    def action_resolve(self):
        self.write({"state": "resolved"})
