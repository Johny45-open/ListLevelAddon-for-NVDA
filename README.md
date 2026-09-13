<div lang = "cs">

# List Level Addon pro NVDA

Tento doplněk vylepšuje čtení informací o seznamech v odečítači obrazovky NVDA.

## Obsah

* [Funkce](#funkce)
* [Nastavení](#nastavení)
* [Klávesové zkratky](#klávesové-zkratky)
* [Změna klávesové zkratky](#změna-klávesové-zkratky)
* [Instalace](#instalace)

---

## Funkce

* Čte počet zanořených seznamů (počet seznamů nad aktuální položkou).
* Čte aktuální úroveň seznamu.
* Čte počet položek v aktuální úrovni.
* Možnost nastavení, co přesně má NVDA oznamovat.
* **NOVÉ v 1.2: Oznamování celkového počtu úrovní v režimu prohlížení (Browse Mode)** – na začátku seznamu oznámí např. „Seznam, 3 úrovně.“

---

## Nastavení

Nastavení najdete v menu:

`NVDA → Možnosti → Nastavení → List Level Addon`

### Režimy oznamování

1. **Samotné seznamy**
   Oznamuje pouze informaci o tom, že jste v seznamu a úroveň zanoření.

2. **Jen úrovně**
   Oznamuje pouze úroveň (např. „úroveň 2“).

3. **Všechno**
   Oznamuje seznam, úroveň i počet položek v této úrovni.

### Režim prohlížení – celkový počet úrovní

Nová volba v témže panelu:

* **Oznamovat celkový počet úrovní v režimu prohlížení (při vstupu do seznamu)**

  - Výchozí stav: **vypnuto** (co nejméně rušivé, uživatel si zapne dle potřeby).
  - Je-li zapnuto, doplněk v Browse Mode (web, HTML dokumenty, Word s Browse Mode) při **vstupu** do seznamu jednou oznámí celkový počet úrovní, např.:

    > „Seznam, 3 úrovně.“

  - Poté pokračuje standardní logika (úroveň, pozice) bez opakování.
  - Hlášení se neopakuje při pohybu uvnitř stejného seznamu, při šipkování tam a zpět ani při pohybu mezi úrovněmi téhož seznamu.
  - Při přechodu do jiného seznamu se oznámí znovu pro nový seznam.
  - Pokud dokument neposkytuje spolehlivou strukturu (např. plain text bez tagů), doplněk nic neoznámí – raději mlčí než hlásí špatný počet.

**Kde to funguje:** Firefox, Chrome, Edge, Brave a další Chromium prohlížeče, HTML dokumenty; částečně Word (pokud používá Browse Mode). V objektovém/Focus Mode zůstává chování beze změny.

**Limity:** Počet úrovní je určen ze skutečné struktury (`ROLE_LIST` hierarchie / `ControlField` `level` / virtuální buffer), ne heuristikou odsazení. Pokud NVDA strukturu neposkytne, doplněk ji neodhaduje.

---

## Klávesové zkratky

| Akce                        | Klávesová zkratka |
| --------------------------- | ----------------- |
| Přepínání režimů oznamování | `NVDA+Alt+L`      |

---

## Změna klávesové zkratky

1. Stiskem `NVDA+N` otevřete nabídku NVDA.
2. Otevřete `Možnosti`.
3. Vyberte `Klávesové příkazy`.
4. Ve stromovém zobrazení přejděte do sekce **Různé**.
5. Najděte položky:

   * `Otevře dialog nastavení doplňku List Level Addon`
   * `Přepíná mezi režimy oznamování informací o seznamech`
6. Rozbalte položku a najděte aktuální zkratku:

   * `NVDA+Shift+Alt+L`
   * `NVDA+Alt+L`
7. Přesuňte se na tlačítko `Odstranit`.
8. Poté použijte tlačítko `Přidat`.
9. Stiskněte novou klávesovou zkratku.
10. Vyberte rozložení klávesnice:

    * desktop
    * laptop
    * obě rozložení
11. Potvrďte tlačítkem `OK`.

---

## Instalace

1. Stáhněte soubor doplňku `.nvda-addon`.
2. Spusťte jej.
3. Potvrďte instalaci do NVDA.
4. Restartujte NVDA.

---

## Technické poznámky (1.2)

* Detekce Browse Mode přes `treeInterceptorHandler.getTreeInterceptor(obj)` + `BrowseModeDocumentTreeInterceptor.passThrough`.
* Výpočet `totalLevels` DFS přes `NVDAObject.children` od nejvnějšího `ROLE_LIST` (max zanoření), cachováno dle `IA2UniqueID`/`uniqueID` na `treeInterceptor` (O(N) jen při vstupu, poté O(1)).
* Fallback `TextInfo.getTextWithFields()` pokud strom neposkytne strukturu.
* Invalidace cache při `treeInterceptorChanged` / `documentLoadComplete` / změně dokumentu.
* Koordinace s NVDA hlášením „seznam“: hlášení doplňku jen při vstupu, ne při každé položce.
