# HealBite Nutrition Reference Catalog Coverage Report

**Catalog ID:** healbite-nutrition-catalog-v2
**Format Version:** 2.2
**Publication Date:** 2026-10-27
**Generated At (UTC):** 2026-10-27T10:31:45.157699+00:00
**SHA-256 Checksum:** `2fbe95c69a15f122177322411b7888ef3020f884acd01eab6126358acbc9a3f7`
**Database Size:** 10379264 bytes (9.90 MB)
**Total Validated Records:** 13,388

---

## 1. Source Breakdown

| Source Dataset | Subsets / Citations | Validated Records | Coverage Focus |
| :--- | :--- | :--- | :--- |
| **USDA FoodData Central** | Foundation Foods, SR Legacy, Survey (FNDDS) | 13,319 | Core macro benchmarks, raw ingredients, prepared dishes, cuts, preparations |
| **Skurikhin & Tutelyan (2008)** | 'Таблицы химического состава российских пищевых продуктов' | 64 | Traditional Russian culinary staples (творог, кефир, гречка, сметана, борщ) |
| **Fineli (THL Finland)** | Release-20 National Food Composition Database | 5 | Northern European bakery, rye staples, oat variants |
| **Total Database Records** | — | **13,388** | **100% offline, local SQLite storage** |

---

## 2. Ingestion & Validation Statistics

- **Total Raw Records Scanned:** 13,610
- **Valid Profiles Ingested:** 13,388
- **Incomplete / Non-Proximate Records Rejected:** 220 (records lacking calories, protein, fat, or carbohydrates)
- **Duplicate Records Suppressed:** 2
- **Zero-Value Invention Policy:** Strictly enforced. No missing nutrients were artificially fabricated or zeroed.

---

## 3. Culinary & Alias Coverage

- **Preparation States Categorized:** Raw, Boiled/Poached, Baked, Roasted, Fried/Sautéed, Grilled/Broiled, Steamed, Braised/Stewed, Smoked, Dried, Canned, Cured/Pickled.
- **Curated Russian Food Mappings & Synonyms:** Over 350+ canonical foods, covering meats, poultry, fish, dairy, grains, vegetables, fruits, legumes, nuts, oils, and prepared dishes.
- **Total Registered Russian Aliases:** Over 1,200 indexed token aliases in `food_aliases`.
- **Query Resolution:** Multi-tier search (Exact ID -> Normalized Alias -> Stemmed Token Match -> Fuzzy Sequence Matcher fallback with >= 0.85 threshold).

---

## 4. Integrity and Compliance

- **Storage Engine:** SQLite 3 (`PRAGMA integrity_check = ok`).
- **Read-Only Enforced:** `mode=ro` runtime access.
- **Network Calls at Lookup:** 0 (strictly zero HTTP/network dependencies).
