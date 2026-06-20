# ICG MSSQL View Schemas

This document captures the schema of the three ICG Manager MSSQL views used as data sources for the sync pipeline.

Source: `dj-icg-prestashop/djangodocker/simpleapp/mssql.py` (legacy repo).

---

## `view_imp_articles` — Products

```
CODARTICULO;Referencia;TALLA;COLOR;CODBARRAS;CODBARRAS2;DESCRIPCION;Tipo_impuesto;IVA;Codproveedor;Nombre_Proveedor;Fecha_Modificado;Visible_Web;Codigo_Marca;Descripcion_Marca;DESCATALOGADO
```

| Index | Column          | Description                         | Example                        |
|-------|-----------------|-------------------------------------|--------------------------------|
| 0     | CODARTICULO     | ICG product ID (unique)             | `7500`                         |
| 1     | Referencia      | Product reference code              | `0930095`                      |
| 2     | TALLA           | Size                                | `12`                           |
| 3     | COLOR           | Color                               | `CAR 12 ML`                    |
| 4     | CODBARRAS       | EAN-13 barcode                      | `8712079312930`                |
| 5     | CODBARRAS2      | Secondary barcode                   | `8712079312800`                |
| 6     | DESCRIPCION     | Product name / description          | `Caja Témpera ArtCreation`     |
| 7     | Tipo_impuesto   | Tax type                            | `1`                            |
| 8     | IVA             | VAT rate (%)                        | `21`                           |
| 9     | Codproveedor    | Supplier code                       | `93`                           |
| 10    | Nombre_Proveedor| Supplier name                       | `TALENS ESPAÑA S.A.U.`        |
| 11    | Fecha_Modificado| Last modified timestamp             | `2020-01-29 13:36:12`          |
| 12    | Visible_Web     | Visible on web (`T`/`F`)            | `T`                            |
| 13    | Codigo_Marca    | Manufacturer code                   | `14000`                        |
| 14    | Descripcion_Marca| Manufacturer name                  | `ARTECREATION`                 |
| 15    | DESCATALOGADO   | Discontinued (`T`/`F`)              | `F`                            |

**Importer mapping** (`_persist_product_row`):

| Row index | Target field                  |
|-----------|-------------------------------|
| 0         | `Product.icg_id`              |
| 1         | `Product.reference`           |
| 2         | `Combination.icg_size`        |
| 3         | `Combination.icg_color`       |
| 4         | `Combination.ean13`           |
| 6         | `Product.name`                |
| 11        | `modified_at` (cursor)        |
| 12        | `Product.visible_web`         |
| 13        | `Manufacturer.icg_code`       |
| 14        | `Manufacturer.name`           |
| 15        | `Product.discontinued`        |

---

## `view_imp_preus` — Prices

```
Tarifa;Codarticulo;Talla;Color;Pbruto_iva;Dto_porc;Pneto_iva;Dto_impote_iva;Iva;Pbruto_s_iva;Pneto_s_iva;Dto_importe_s_iva;Fecha_modificado
```

| Index | Column             | Description                          | Example              |
|-------|--------------------|--------------------------------------|----------------------|
| 0     | Tarifa             | Price list / tariff code             | `1`                  |
| 1     | Codarticulo        | ICG product ID                       | `7498`               |
| 2     | Talla              | Size                                 | `***`                |
| 3     | Color              | Color                                | `***`                |
| 4     | Pbruto_iva         | Gross price (incl. VAT)              | `135.45`             |
| 5     | Dto_porc           | Discount percentage                  | `30`                 |
| 6     | Pneto_iva          | Net price (incl. VAT)                | `94.815`             |
| 7     | Dto_impote_iva     | Discount amount (incl. VAT)          | `40.635`             |
| 8     | Iva                | VAT rate (%)                         | `21`                 |
| 9     | Pbruto_s_iva       | Gross price (excl. VAT)              | `111.94`             |
| 10    | Pneto_s_iva        | Net price (excl. VAT)                | `78.36`              |
| 11    | Dto_importe_s_iva  | Discount amount (excl. VAT)          | `33.58`              |
| 12    | Fecha_modificado   | Last modified timestamp              | `2020-01-20 16:55:35`|

**Importer mapping** (`_persist_price_row`):

| Row index | Target field                      |
|-----------|-----------------------------------|
| 1         | `Product.icg_id` (lookup)         |
| 2         | `Combination.icg_size` (lookup)   |
| 3         | `Combination.icg_color` (lookup)  |
| 5         | `Product.discount_percent`        |
| 8         | `Price.vat_rate`                  |
| 10        | `Price.amount_ex_vat`             |
| 12        | `modified_at` (cursor)            |

---

## `view_imp_stocks` — Stocks

```
Codarticulo;Talla;Color;Codalmacen;Nombre_alm;Stock_real;Stock_Aservir;Stock_disponible;Fecha_Modificado
```

| Index | Column             | Description                          | Example              |
|-------|--------------------|--------------------------------------|----------------------|
| 0     | Codarticulo        | ICG product ID                       | `7498`               |
| 1     | Talla              | Size                                 | `***`                |
| 2     | Color              | Color                                | `***`                |
| 3     | Codalmacen         | Warehouse code                       | `01`                 |
| 4     | Nombre_alm         | Warehouse name                       | `Pintor Fortuny`     |
| 5     | Stock_real         | Real stock                           | `5`                  |
| 6     | Stock_Aservir      | Stock to serve                        | `0`                  |
| 7     | Stock_disponible   | Available stock                      | `5`                  |
| 8     | Fecha_Modificado   | Last modified timestamp              | `2020-03-06 19:24:47`|

**Importer mapping** (`_persist_stock_row`):

| Row index | Target field                      |
|-----------|-----------------------------------|
| 0         | `Product.icg_id` (lookup)         |
| 1         | `Combination.icg_size` (lookup)   |
| 2         | `Combination.icg_color` (lookup)  |
| 3         | `Stock.warehouse_code`            |
| 7         | `Stock.quantity`                  |
| 8         | `modified_at` (cursor)            |

**Note:** Only warehouse `01` is imported; all others are skipped.

---

## Notes

- All three views are queried with `SELECT *` and cursor-based pagination on `Fecha_Modificado` / `Fecha_modificado`.
- The `***` value in Talla/Color means "no value" (default combination).
- `Dto_porc` (discount %) is an integer in ICG (e.g. `30` = 30%).
- `amount_ex_vat` in the importer maps to `Pneto_s_iva` (net price excl. VAT), despite the field name suggesting gross.
