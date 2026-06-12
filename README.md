# CommerceBridge Magento 2 for Odoo 19

CommerceBridge Magento 2 is an operations-grade Magento 2 / Adobe Commerce connector for Odoo 19.

This package is designed as a strong foundation, not a cheap direct clone of existing connectors. It focuses on safety, mapping, recoverability, and non-technical operation.

## Implemented in this package

- Magento instance setup with URL/token/SSL/timeout controls
- Magento connection test
- Website, store group, and store-view import
- Guided sync wizard
- Mapping Center for payment, shipping, tax, order status, warehouse/source, attributes, pricelists, B2B company, and shared catalog planning
- Readiness score and checks before go-live
- Queue job engine with pending/running/done/failed/retry/block states
- Retry with exponential backoff
- Payload/response logging
- Error explanation hints for non-technical users
- Product/category import foundation
- Simple/configurable/other product binding records
- Odoo product auto-create option
- Customer/partner binding
- Magento order import foundation
- Guest/registered customer handling
- Shipping line handling
- Discount handling
- Order total guardrail with block/adjust/warn policies
- Inventory stock export from Odoo to Magento
- MSI source-item mode and classic stock-item mode
- Safety stock deduction
- Inventory drift report
- Delivery order button to create Magento shipment export job
- Shipment export job using Magento order item mapping
- Default Magento Shipping and Adjustment service products
- Manager/user security groups

## Advanced scaffolding included but not fully live yet

These are represented in models/settings/mapping screens so they can be expanded without redesigning the module:

- Adobe Commerce B2B company mapping
- Shared catalog/pricelist mapping
- Product attribute mapping
- Price/special price/tier price export operation placeholders
- Bulk API optimization path
- Magento companion extension/webhook path

## Installation

1. Copy `commerce_bridge_magento2` into your Odoo 19 custom addons path.
2. Restart Odoo.
3. Update Apps List.
4. Install **CommerceBridge Magento 2**.
5. Give your user the **CommerceBridge Manager** group.
6. Open **CommerceBridge → Connections**.

## Recommended first run

1. Create a Magento connection.
2. Add Magento base URL and integration/admin token.
3. Click **Test Connection**.
4. Click **Import Stores**.
5. Click **Check Readiness**.
6. Fix blocking readiness checks.
7. Click **Import Products**.
8. Process queue.
9. Click **Import Orders**.
10. Process queue.
11. Check Inventory Drift.
12. Export stock after confirming quantities and source mapping.

## Important notes

- This version is API-first. It does not require installing a Magento extension.
- For very large stores, add bulk API execution after testing core jobs with a real Magento database.
- For B2B shared catalog/company sync, validate the Adobe Commerce B2B modules first.
- Do not run stock export blindly on production. Run Readiness and Inventory Drift first.

## Technical name

`commerce_bridge_magento2`
