import globalPluginHandler
import controlTypes
import config
import gui
from gui.settingsDialogs import SettingsPanel, NVDASettingsDialog
import wx
import speech
import addonHandler
import logHandler
import gettext
import core
import io
import textInfos

# Inicializace překladů pro doplňku
_ = gettext.gettext

# Definice konfiguračního schématu
confspec = """
[listLevelAddon]
    mode = string(default='everything')
    announceBrowseModeTotalLevels = boolean(default=false)
"""

# ---------------------------------------------------------------------------
# Sdílené helpery – společná abstrakce pro Focus i Browse Mode
# ---------------------------------------------------------------------------

def _get_tree_interceptor(obj):
    """Vrátí treeInterceptor pro daný NVDAObject nebo None."""
    try:
        ti = getattr(obj, 'treeInterceptor', None)
        if ti is not None:
            return ti
    except:
        pass
    try:
        import treeInterceptorHandler
        return treeInterceptorHandler.getTreeInterceptor(obj)
    except:
        return None


def _is_browse_mode(obj, ti=None):
    """Rozliší Browse Mode (virtualBuffer) vs Focus Mode.
    Vrací (isBrowse:bool, treeInterceptor).
    passThrough==True => Focus Mode, False => Browse Mode.
    Ověřuje navíc BrowseModeDocumentTreeInterceptor.
    """
    try:
        if ti is None:
            ti = _get_tree_interceptor(obj)
        if ti is None:
            return False, None
        if getattr(ti, 'passThrough', False):
            return False, ti
        try:
            import browseMode
            if isinstance(ti, browseMode.BrowseModeDocumentTreeInterceptor):
                return True, ti
        except:
            # browseMode není dostupný (testy) – spoléhej na passThrough
            return True, ti
        return False, ti
    except Exception as e:
        logHandler.log.debug(f"ListLevelAddon _is_browse_mode error: {e}")
        return False, ti


def _get_caret_object(ti, fallback_obj):
    """Získá NVDAObject na pozici caret v Browse Mode.
    Vrací (caret_obj, caret_info) kde caret_info je TextInfo na caret.
    V Focus Mode vrací fallback_obj.
    """
    try:
        info = ti.makeTextInfo(textInfos.POSITION_CARET)
        caret_obj = None
        try:
            caret_obj = info.NVDAObjectAtStart
        except:
            pass
        if caret_obj is None:
            try:
                base = getattr(info, 'basePosition', None)
                if base is not None and hasattr(base, 'role'):
                    caret_obj = base
            except:
                caret_obj = None
        if caret_obj is not None:
            return caret_obj, info
    except Exception as e:
        logHandler.log.debug(f"ListLevelAddon _get_caret_object error: {e}")
    return fallback_obj, None


def _find_outermost_list(caret_obj):
    """Najde nejvnější LIST ancestor (kořen seznamu)."""
    outermost = None
    temp = caret_obj
    depth = 0
    MAX_DEPTH = 20
    while temp is not None and depth < MAX_DEPTH:
        try:
            role = getattr(temp, 'role', None)
        except:
            role = None
        if role == controlTypes.ROLE_LIST:
            outermost = temp
        try:
            temp = temp.parent
        except:
            break
        depth += 1
    return outermost


def _get_list_unique_id(list_obj):
    """Stabilní ID pro LIST objekt pro cache a deduplikaci."""
    for attr in ('IA2UniqueID', 'uniqueID', 'IAccessibleObject', 'windowHandle'):
        try:
            val = getattr(list_obj, attr, None)
            if val is not None:
                if attr == 'IAccessibleObject':
                    uid = getattr(list_obj, 'IA2UniqueID', None)
                    if uid is not None:
                        return uid
                    return id(val)
                return val
        except:
            continue
    return id(list_obj)


def _count_list_ancestors(obj):
    """Spočítá počet ROLE_LIST předků (včetně samotného obj pokud je LIST).
    Vrací (count, outermost) – count je hloubka zanoření.
    Používá parent chain, funguje pro Focus Mode i pro virtual caret_obj v Browse Mode.
    """
    count = 0
    outermost = None
    temp = obj
    depth = 0
    MAX_DEPTH = 20
    while temp is not None and depth < MAX_DEPTH:
        try:
            role = getattr(temp, 'role', None)
        except:
            role = None
        if role == controlTypes.ROLE_LIST:
            count += 1
            outermost = temp
        try:
            temp = temp.parent
        except:
            break
        depth += 1
    return count, outermost


# ---------------------------------------------------------------------------
# Vrstva 2/3 – TextInfo / ControlField fallback (strukturovaný, bez heuristiky)
# ---------------------------------------------------------------------------

def _get_fields_list(info, force_report_lists=True):
    """Bezpečně získá list z info.getTextWithFields().
    Vrací list nebo None. Vynucuje reportLists=True aby nezáviselo na uživatelském nastavení.
    Pure helper – volá se pouze při miss vrstvy 1 (výkon).
    """
    if info is None:
        return None
    try:
        # zkus s formatConfig
        if force_report_lists:
            try:
                # NVDA API: getTextWithFields(formatConfig: dict | None)
                return info.getTextWithFields({"reportLists": True})
            except TypeError:
                # některé implementace neberou argument
                return info.getTextWithFields()
        else:
            return info.getTextWithFields()
    except Exception as e:
        logHandler.log.debug(f"ListLevelAddon _get_fields_list error: {e}")
        return None


def _scan_fields_for_list(fields, max_items=500):
    """Prohledá getTextWithFields() výstup pro strukturované info o seznamu.

    Vrací dict: {in_list:bool, list_count:int, level:int|None, max_depth:int, has_list:bool}
    - in_list True pokud controlStart ROLE_LIST nebo ROLE_LISTITEM
    - list_count = aktuální hloubka zanoření v místě caret (počet LIST na stacku)
    - level = nejhlubší field['level'] pokud je int, jinak list_count
    - max_depth = maximální hloubka LIST v prohledaném úseku (pro totalLevels fallback)

    Žádná heuristika z textu/odsazení – pouze role+level z ControlField.
    """
    if not fields:
        return {"in_list": False, "list_count": 0, "level": None, "max_depth": 0, "has_list": False}
    in_list = False
    has_list = False
    depth = 0
    max_depth = 0
    level = None
    # omezit počet pro výkon
    limit = min(len(fields), max_items)
    for i in range(limit):
        item = fields[i]
        if isinstance(item, str):
            continue
        try:
            cmd = getattr(item, "command", None)
            field = getattr(item, "field", None)
            # Dict přímý (některé test mocky)
            if field is None and isinstance(item, dict):
                field = item
                # odhad cmd podle presence role
                if field.get("role") is not None:
                    cmd = "controlStart"
            if field is None or cmd is None:
                continue
            role = field.get("role")
            if cmd == "controlStart":
                if role == controlTypes.ROLE_LIST:
                    has_list = True
                    in_list = True
                    depth += 1
                    if depth > max_depth:
                        max_depth = depth
                    lvl = field.get("level")
                    if isinstance(lvl, int) and lvl > 0:
                        # nejhlubší level je nejpřesnější
                        level = lvl
                elif role == controlTypes.ROLE_LISTITEM:
                    has_list = True
                    in_list = True
            elif cmd == "controlEnd":
                if role == controlTypes.ROLE_LIST and depth > 0:
                    depth -= 1
        except Exception:
            continue
    # list_count je hloubka v místě caret (depth po zpracování ancestors)
    # Pro collapsed caret je depth na konci = počet otevřených LIST
    # Pokud level nebyl v poli, použij depth
    list_count = depth if depth > 0 else (max_depth if has_list else 0)
    # Pokud level je int, preferuj ho, jinak list_count
    effective_level = level if isinstance(level, int) and level > 0 else (list_count if has_list else None)
    return {
        "in_list": in_list,
        "has_list": has_list,
        "list_count": list_count,
        "level": effective_level,
        "max_depth": max_depth,
        "raw_level": level,
    }


def _get_list_info_from_fields(info):
    """Vrstva 2 helper – z TextInfo.getTextWithFields() odvodí (in_list, list_count, level, max_depth)."""
    fields = _get_fields_list(info, force_report_lists=True)
    if fields is None:
        return None
    res = _scan_fields_for_list(fields, max_items=500)
    if not res["has_list"]:
        return None
    return res


def _get_total_levels_from_fields(ti, caret_info, max_scan=10000):
    """Fallback pro totalLevels přes ControlField.

    Prochází celý dokument (POSITION_ALL) a počítá max zanoření LIST.
    Pro více oddělených seznamů se snaží určit segment obsahující caret
    (podle textu caret a struktury), aby nevrátil globální max jiného seznamu.
    Vrací int nebo None. Volá se pouze při miss DFS a pouze v BrowseMode.
    Omezeno na max_scan field items pro výkon.
    """
    if ti is None:
        return None
    try:
        try:
            all_info = ti.makeTextInfo(textInfos.POSITION_ALL)
        except Exception:
            all_info = caret_info
        fields = _get_fields_list(all_info, force_report_lists=True)
        if not fields:
            return None
        # Rychlý globální scan pro max_depth
        res = _scan_fields_for_list(fields, max_items=max_scan)
        if not res["max_depth"] or res["max_depth"] < 1:
            return None
        caret_res = _get_list_info_from_fields(caret_info) if caret_info else None
        if not caret_res or not caret_res.get("has_list"):
            return res["max_depth"]
        # Pokus o segment-aware výpočet – rozdělit dokument na outermost segmenty
        try:
            # Sestav segmenty (každý outermost LIST)
            segments = []  # list of {start, end, maxDepth, firstText}
            depth = 0
            seg_max = 0
            seg_start = None
            seg_first_text = ""
            limit = min(len(fields), max_scan)
            for idx in range(limit):
                item = fields[idx]
                if isinstance(item, str):
                    if depth > 0 and not seg_first_text and item.strip():
                        seg_first_text = item.strip()[:40]
                    continue
                cmd = getattr(item, "command", None)
                field = getattr(item, "field", None)
                if field is None and isinstance(item, dict):
                    field = item
                    if field.get("role") is not None:
                        cmd = "controlStart"
                if field is None or cmd is None:
                    continue
                role = field.get("role")
                if cmd == "controlStart" and role == controlTypes.ROLE_LIST:
                    if depth == 0:
                        seg_start = idx
                        seg_max = 1
                        seg_first_text = ""
                    else:
                        seg_max = max(seg_max, depth + 1)
                    depth += 1
                    if depth > seg_max:
                        seg_max = depth
                elif cmd == "controlEnd" and role == controlTypes.ROLE_LIST:
                    depth -= 1
                    if depth == 0 and seg_start is not None:
                        segments.append({"start": seg_start, "end": idx, "maxDepth": seg_max, "firstText": seg_first_text})
                        seg_start = None
                        seg_max = 0
                    if depth < 0:
                        depth = 0
            # Najdi segment obsahující caret – podle textu caret
            caret_text = ""
            try:
                # zkus získat text kolem caret (řádka)
                caret_text = caret_info.text.strip()[:50] if caret_info and hasattr(caret_info, "text") else ""
                if not caret_text and caret_info:
                    # fallback expand line
                    try:
                        tmp = caret_info.copy()
                        tmp.expand(textInfos.UNIT_LINE)
                        caret_text = tmp.text.strip()[:50]
                    except:
                        caret_text = ""
            except:
                caret_text = ""
            if caret_text and segments:
                # najdi segment kde se caret_text vyskytuje
                for seg in segments:
                    # prohledat text uvnitř segmentu
                    seg_text_combined = ""
                    for j in range(seg["start"], min(seg["end"] + 1, limit)):
                        it = fields[j]
                        if isinstance(it, str):
                            seg_text_combined += it + " "
                            if len(seg_text_combined) > 500:
                                break
                    if caret_text and caret_text[:20] in seg_text_combined:
                        return seg["maxDepth"]
                    # fallback: pokud caret_res level je uvnitř segment max
                    # (není spolehlivé, ale lepší než globální)
                # nenašli podle textu – vrať globální max s warningem
                logHandler.log.debug(f"ListLevelAddon segment-aware: caret_text '{caret_text[:20]}' nenalezen v segmentech, vracím globální max {res['max_depth']}")
            # pokud caret_res hloubka > globální max, vrať caret hloubku
            if caret_res["list_count"] and res["max_depth"] < caret_res["list_count"]:
                return caret_res["list_count"]
            return res["max_depth"]
        except Exception as e:
            logHandler.log.debug(f"ListLevelAddon segment-aware fallback error: {e}")
            if caret_res["list_count"] and res["max_depth"] < caret_res["list_count"]:
                return caret_res["list_count"]
            return res["max_depth"]
    except Exception as e:
        logHandler.log.debug(f"ListLevelAddon _get_total_levels_from_fields error: {e}")
    return None


def _get_field_segment_id(ti, caret_info, full_fields, max_scan=10000):
    """Vrátí stabilní segment id pro ControlField fallback (pro cache/dedup).
    Používá segmentaci dokumentu na outermost LISTy a hledá segment obsahující caret.
    """
    try:
        ti_id = id(ti) if ti is not None else 0
        # rychlá cesta – runtimeID
        if caret_info:
            fields_caret = _get_fields_list(caret_info, force_report_lists=True)
            if fields_caret:
                for it in fields_caret:
                    f = getattr(it, "field", None)
                    if f is None and isinstance(it, dict):
                        f = it
                    if f and f.get("role") == controlTypes.ROLE_LIST:
                        rid = f.get("runtimeID") or f.get("uniqueID")
                        if rid:
                            return rid
        # segment-aware
        if full_fields is None and ti is not None:
            try:
                all_info = ti.makeTextInfo(textInfos.POSITION_ALL)
                full_fields = _get_fields_list(all_info, force_report_lists=True)
            except:
                full_fields = None
        if not full_fields or not caret_info:
            return f"field:{ti_id}"
        # sestav segmenty
        segments = []
        depth = 0
        seg_start = None
        limit = min(len(full_fields), max_scan)
        for idx in range(limit):
            item = full_fields[idx]
            if isinstance(item, str):
                continue
            cmd = getattr(item, "command", None)
            field = getattr(item, "field", None)
            if field is None and isinstance(item, dict):
                field = item
                if field.get("role") is not None:
                    cmd = "controlStart"
            if field is None or cmd is None:
                continue
            role = field.get("role")
            if cmd == "controlStart" and role == controlTypes.ROLE_LIST and depth == 0:
                seg_start = idx
            if cmd == "controlStart" and role == controlTypes.ROLE_LIST:
                depth += 1
            elif cmd == "controlEnd" and role == controlTypes.ROLE_LIST:
                depth -= 1
                if depth == 0 and seg_start is not None:
                    segments.append((seg_start, idx))
                    seg_start = None
                if depth < 0:
                    depth = 0
        # najdi caret pozici
        caret_text = ""
        try:
            caret_text = caret_info.text.strip()[:50] if hasattr(caret_info, "text") else ""
            if not caret_text:
                tmp = caret_info.copy()
                tmp.expand(textInfos.UNIT_LINE)
                caret_text = tmp.text.strip()[:50]
        except:
            caret_text = ""
        if caret_text and segments:
            for s_idx, (s, e) in enumerate(segments):
                seg_text = ""
                for j in range(s, min(e + 1, limit)):
                    it = full_fields[j]
                    if isinstance(it, str):
                        seg_text += it + " "
                        if len(seg_text) > 500:
                            break
                if caret_text[:20] in seg_text:
                    return f"fieldSeg:{ti_id}:{s_idx}:{s}"
        return f"field:{ti_id}:{len(segments)}"
    except Exception:
        try:
            return f"field:{id(ti)}"
        except:
            return "field:0"


def get_list_level(obj, ti=None):
    """Společná abstrakce pro aktuální úroveň seznamu.
    Automaticky překládá dokument -> caret_obj v Browse Mode.
    Priority:
      1) NVDAObject parent-chain count + positionInfo/level (robustní pro Gecko kde level neexistuje)
      2) BrowseMode TextInfo.getTextWithFields() ControlField role/level (vrstva 2)
      3) FocusMode TextInfo.getTextWithFields() (vrstva 3)
      4) Fallback: parent ROLE_LIST count
    Vrací int (1-based) nebo None pokud nelze spolehlivě určit / není v seznamu.
    """
    # Překlad v Browse Mode – získej caret_obj i caret_info pro fallback
    caret_info = None
    try:
        is_browse, ti_resolved = _is_browse_mode(obj, ti)
        if is_browse and ti_resolved is not None:
            caret_obj, ci = _get_caret_object(ti_resolved, obj)
            if ci is not None:
                caret_info = ci
            # Použij caret_obj pokud se liší od dokumentu
            if caret_obj is not obj:
                obj = caret_obj
                ti = ti_resolved
    except:
        pass

    # Vrstva 1: objektová hierarchie
    count, outermost = _count_list_ancestors(obj)
    if count != 0 and outermost is not None:
        # Zkus autoritativní level z positionInfo / level atributu (UIA, IA2 groupPosition)
        temp = obj
        depth = 0
        MAX_DEPTH = 20
        while temp is not None and depth < MAX_DEPTH:
            try:
                lvl = getattr(temp, 'level', None)
                if isinstance(lvl, int) and lvl > 0:
                    return lvl
            except:
                pass
            try:
                pos = getattr(temp, 'positionInfo', None)
                if pos:
                    lvl = pos.get('level')
                    if isinstance(lvl, int) and lvl > 0:
                        return lvl
            except:
                pass
            try:
                temp = temp.parent
            except:
                break
            depth += 1
        # Fallback – spolehlivý count předků (Gecko/IA2 bez level)
        return count

    # Vrstva 2/3: TextInfo ControlField fallback – pouze strukturované, žádná heuristika
    # Nejprve BrowseMode caret_info
    if caret_info is not None:
        try:
            res = _get_list_info_from_fields(caret_info)
            if res and res["has_list"] and res["level"] is not None:
                return res["level"]
        except Exception:
            pass
    # Vrstva 3: FocusMode / obecný TextInfo z obj
    try:
        # Zkus získat TextInfo přímo z obj (UIA TextPattern, IA2)
        info = None
        try:
            if hasattr(obj, "makeTextInfo"):
                info = obj.makeTextInfo(textInfos.POSITION_CARET)
        except:
            info = None
        if info is not None and info is not caret_info:
            res = _get_list_info_from_fields(info)
            if res and res["has_list"] and res["level"] is not None:
                return res["level"]
    except Exception:
        pass

    return None


def get_list_depth(outermost):
    """Společná abstrakce pro celkový počet úrovní (max hloubka) daného seznamu.
    DFS přes NVDAObject.children od nejvnějšího LISTu.
    Vrací int >=1 nebo None pokud nelze určit.
    Pure funkce – necachuje, cache řeší volající.
    """
    if outermost is None:
        return None
    max_depth = 1
    stack = [(outermost, 1)]
    visited = set()
    visited.add(id(outermost))
    # Ochrana proti obrovským dokumentům
    MAX_VISITED = 10000
    while stack:
        if len(visited) > MAX_VISITED:
            logHandler.log.debug("ListLevelAddon get_list_depth: MAX_VISITED reached, truncating")
            break
        node, cur_depth = stack.pop()
        if cur_depth > max_depth:
            max_depth = cur_depth
            if max_depth > 20:
                max_depth = 20
                # Nezkracuj prohledávání, jen omez výsledek
        try:
            children = getattr(node, 'children', None)
            if children is None:
                continue
            children = list(children)
        except:
            continue
        for child in children:
            cid = id(child)
            if cid in visited:
                continue
            visited.add(cid)
            try:
                role = getattr(child, 'role', None)
            except:
                role = None
            if role == controlTypes.ROLE_LIST:
                new_depth = cur_depth + 1
                if new_depth > max_depth:
                    max_depth = new_depth
                stack.append((child, new_depth))
            elif role == controlTypes.ROLE_LISTITEM:
                stack.append((child, cur_depth))
            else:
                # Obecný kontejner – může skrývat další LIST
                stack.append((child, cur_depth))
    return max_depth


class ListLevelSettingsPanel(SettingsPanel):
    title = _("List Level Addon")
    
    def makeSettings(self, settingsSizer):
        # Vyčištění sizeru před přidáním prvků
        settingsSizer.Clear(delete_windows=True)
        
        self.label = wx.StaticText(self, wx.ID_ANY, _("Co má NVDA číst o seznamech:"))
        settingsSizer.Add(self.label)
        
        self.choices = [
            ("nothing", _("Nic")),
            ("lists_only", _("Samotné seznamy")),
            ("levels_only", _("Jen úrovně")),
            ("position_only", _("Jen pozice (X z Y)")),
            ("everything", _("Všechno"))
        ]
        
        self.mode_choice = wx.Choice(self, wx.ID_ANY, choices=[c[1] for c in self.choices])
        settingsSizer.Add(self.mode_choice)
        
        try:
            current_mode = config.conf["listLevelAddon"].get("mode", "everything")
        except:
            current_mode = "everything"
            
        for i, (value, label) in enumerate(self.choices):
            if value == current_mode:
                self.mode_choice.SetSelection(i)
                break

        # Nové nastavení pro Browse Mode
        try:
            browse_val = config.conf["listLevelAddon"].get("announceBrowseModeTotalLevels", False)
        except:
            browse_val = False
        self.browseModeCheckbox = wx.CheckBox(
            self, wx.ID_ANY,
            _("Oznamovat celkový počet úrovní v režimu prohlížení (při vstupu do seznamu)")
        )
        self.browseModeCheckbox.SetValue(bool(browse_val))
        settingsSizer.Add(self.browseModeCheckbox, flag=wx.TOP, border=10)

        helpText = _("Hlášení: \"Seznam, 3 úrovně.\" pouze při vstupu do seznamu, ne při každé položce.")
        self.helpLabel = wx.StaticText(self, wx.ID_ANY, helpText)
        # menší font pro nápovědu
        try:
            font = self.helpLabel.GetFont()
            font.SetPointSize(max(8, font.GetPointSize() - 1))
            self.helpLabel.SetFont(font)
        except:
            pass
        settingsSizer.Add(self.helpLabel)

    def onSave(self):
        selection = self.mode_choice.GetSelection()
        if selection != wx.NOT_FOUND:
            config.conf["listLevelAddon"]["mode"] = self.choices[selection][0]
        try:
            config.conf["listLevelAddon"]["announceBrowseModeTotalLevels"] = bool(self.browseModeCheckbox.GetValue())
        except:
            pass

class ListLevelLogic:
    _last_info = None
    _last_obj_id = None
    _last_list_id = None
    _last_level = None

    @staticmethod
    def get_list_info(obj):
        # Kontrola, zda není doplněk vypnutý přes režim
        mode = config.conf["listLevelAddon"].get("mode", "everything")
        if mode == "nothing":
            return None, None, None, None, None

        # Překlad Browse Mode dokument -> caret_obj pokud je potřeba
        # Volající event_caret už překládá, ale pro robustnost (volání z jiných míst)
        effective_obj = obj
        is_browse = False
        ti = None
        caret_info = None
        try:
            is_browse, ti = _is_browse_mode(obj)
            if is_browse and ti is not None:
                caret_obj, ci = _get_caret_object(ti, obj)
                caret_info = ci
                if caret_obj is not obj:
                    effective_obj = caret_obj
        except:
            effective_obj = obj
            is_browse = False
            ti = None

        in_list = False
        item_count = None
        current_index = None
        list_count = 0
        list_id = None
        # level budeme určovat přes get_list_level (robustní)
        effective_level = None
        
        temp = effective_obj
        depth = 0
        MAX_DEPTH = 20
        
        while temp and depth < MAX_DEPTH:
            try:
                role = getattr(temp, 'role', None)
            except:
                role = None
            if role == controlTypes.ROLE_LIST:
                in_list = True
                list_count += 1
                if list_id is None:
                    list_id = _get_list_unique_id(temp)
            
            if role == controlTypes.ROLE_LISTITEM:
                in_list = True
            
            if item_count is None:
                try:
                    ic = getattr(temp, 'itemCount', None)
                    if ic is not None:
                        item_count = ic
                except:
                    pass
            if current_index is None:
                try:
                    idx = getattr(temp, 'indexInGroup', None)
                    if idx is not None:
                        current_index = idx
                except:
                    pass
            
            try:
                pos = getattr(temp, 'positionInfo', None)
                if pos:
                    if item_count is None:
                        ic = pos.get('similarItemsInGroup')
                        if ic is not None:
                            item_count = ic
                    if current_index is None:
                        idx = pos.get('indexInGroup')
                        if idx is not None:
                            current_index = idx
            except:
                pass
            
            try:
                temp = temp.parent
            except:
                break
            depth += 1

        if not in_list:
            # Vrstva 2: BrowseMode ControlField fallback
            fallback_res = None
            if is_browse and caret_info is not None:
                try:
                    fallback_res = _get_list_info_from_fields(caret_info)
                except Exception:
                    fallback_res = None
            if fallback_res and fallback_res.get("has_list"):
                in_list = True
                list_count = fallback_res.get("list_count", 0) or 0
                # synthetic list_id – stabilní v rámci ti
                try:
                    ti_id = id(ti) if ti is not None else id(caret_info) if caret_info else id(effective_obj)
                except:
                    ti_id = id(effective_obj)
                # zkus runtimeID z pole pokud existuje (první LIST)
                rid = None
                try:
                    fields = _get_fields_list(caret_info)
                    if fields:
                        for it in fields:
                            f = getattr(it, "field", None)
                            if f is None and isinstance(it, dict):
                                f = it
                            if f and f.get("role") == controlTypes.ROLE_LIST:
                                rid = f.get("runtimeID") or f.get("uniqueID")
                                if rid:
                                    break
                except:
                    rid = None
                if rid is not None:
                    list_id = rid
                else:
                    # fallback synthetic – odvodí se z hloubky a ti
                    # nutno rozlišit různé seznamy → použij také hash první LIST level
                    list_id = f"field:{ti_id}:{list_count}:{fallback_res.get('level')}"
                # převzít level z fields pro pozdější použití
                if effective_level is None:
                    effective_level = fallback_res.get("level")
            else:
                # Vrstva 3: FocusMode / obecný TextInfo fallback (UIA, IA2)
                try:
                    info = None
                    if hasattr(effective_obj, "makeTextInfo"):
                        try:
                            info = effective_obj.makeTextInfo(textInfos.POSITION_CARET)
                        except:
                            info = None
                    if info is not None and info is not caret_info:
                        fr = _get_list_info_from_fields(info)
                        if fr and fr.get("has_list"):
                            in_list = True
                            list_count = fr.get("list_count", 0) or 0
                            if effective_level is None:
                                effective_level = fr.get("level")
                            # synthetic id pro FocusMode
                            try:
                                list_id = f"field:focus:{id(effective_obj)}:{list_count}:{fr.get('level')}"
                            except:
                                list_id = f"field:focus:{list_count}"
                        else:
                            return None, None, None, None, None
                    else:
                        return None, None, None, None, None
                except Exception:
                    return None, None, None, None, None

        # Robustní určení úrovně – společná abstrakce
        # get_list_level už řeší Browse vs Focus a preference positionInfo > ancestor count
        # Pokud už máme level z fields, ponecháme; jinak zkus get_list_level
        if effective_level is None:
            try:
                effective_level = get_list_level(effective_obj)
            except:
                effective_level = None
        # Fallback pokud get_list_level vrátilo None ale list_count >0 (Gecko bez positionInfo)
        if effective_level is None:
            effective_level = list_count if list_count > 0 else None

        list_msgs = []
        if mode in ('lists_only', 'everything'):
            list_msgs.append(_("Seznam, zanoření {count}").format(count=list_count))
        
        level_msg = None
        if mode in ('levels_only', 'everything'):
            display_level = effective_level if effective_level is not None else list_count
            level_msg = _("úroveň {level}").format(level=display_level)

        pos_msg = None
        if mode in ('position_only', 'everything'):
            if current_index and item_count:
                pos_msg = _("{current} z {total} položek").format(current=current_index, total=item_count)
            elif mode == 'position_only' and item_count:
                pos_msg = _("{count} položek").format(count=item_count)

        return ", ".join(list_msgs), level_msg, pos_msg, list_id, effective_level

    @classmethod
    def check_and_speak(cls, obj):
        try:
            obj_id = (getattr(obj, 'IAccessibleObject', None), getattr(obj, 'UIAElement', None))
        except:
            obj_id = obj

        list_info, level_info, pos_info, list_id, level = cls.get_list_info(obj)
        
        if list_info is None and level_info is None and pos_info is None:
            cls._last_info = None
            cls._last_obj_id = None
            cls._last_list_id = None
            cls._last_level = None
            return

        if obj_id == cls._last_obj_id:
            return

        parts = []
        if list_info: parts.append(list_info)
        
        if level_info and level != cls._last_level:
            parts.append(level_info)
            cls._last_level = level
        
        if pos_info: parts.append(pos_info)
        
        final_msg = ", ".join(parts)
        
        if final_msg != cls._last_info or list_id != cls._last_list_id:
            if final_msg:
                speech.speakMessage(final_msg)
            cls._last_info = final_msg
            cls._last_list_id = list_id

        cls._last_obj_id = obj_id


def _get_cz_levels_text(count):
    """Vrátí správný český tvar pro 'úroveň/úrovně/úrovní'."""
    try:
        n = int(count)
    except:
        return _("{count} úrovní").format(count=count)
    if n == 1:
        return _("1 úroveň")
    elif 2 <= n <= 4:
        return _("{count} úrovně").format(count=n)
    else:
        return _("{count} úrovní").format(count=n)


class BrowseModeListDetector:
    """Detekce celkového počtu úrovní seznamu v Browse Mode.

    Vrstva 1: NVDAObject strom (DFS) od nejvnějšího LISTu – primární zdroj.
    Vrstva 2: ControlField / getTextWithFields fallback – pokud parent chain
             neobsahuje ROLE_LIST (plochý virtual buffer), použije strukturované
             ControlField role/level z getTextWithFields.
    Žádná heuristika z textu/odsazení – pouze strukturované NVDA API.
    Cache: outermost list uniqueID (nebo synthetic field id) -> totalLevels.
    Hlášení pouze při vstupu do seznamu (změna outermostListId).
    """

    _cache = {}
    _cache_ti_id = None
    _last_browse_list_id = None
    _last_browse_obj_id = None

    @classmethod
    def _is_browse_mode(cls, obj, ti=None):
        # Delegace na modulovou funkci
        return _is_browse_mode(obj, ti)

    @classmethod
    def _get_tree_interceptor(cls, obj):
        return _get_tree_interceptor(obj)

    @classmethod
    def _get_caret_object(cls, ti, fallback_obj):
        return _get_caret_object(ti, fallback_obj)

    @classmethod
    def _find_outermost_list(cls, caret_obj):
        return _find_outermost_list(caret_obj)

    @classmethod
    def _get_list_unique_id(cls, list_obj):
        return _get_list_unique_id(list_obj)

    @classmethod
    def _compute_total_levels_via_objects(cls, outermost):
        return get_list_depth(outermost)

    @classmethod
    def _get_total_levels(cls, ti, caret_obj, caret_info):
        outermost = _find_outermost_list(caret_obj)
        # Cache invalidace při změně dokumentu
        ti_id = id(ti) if ti is not None else None
        if cls._cache_ti_id != ti_id:
            cls._cache.clear()
            cls._cache_ti_id = ti_id
            cls._last_browse_list_id = None

        if outermost is not None:
            list_id = _get_list_unique_id(outermost)
            if list_id in cls._cache:
                return cls._cache[list_id], list_id
            # Spočítej DFS – primární zdroj
            total = get_list_depth(outermost)
            if total is not None and total >= 1:
                if total > 20:
                    total = 20
                cls._cache[list_id] = total
                return total, list_id
            # DFS selhalo i přes existenci outermost → zkus ControlField fallback
            logHandler.log.debug("ListLevelAddon BrowseMode: DFS selhalo, zkouším ControlField fallback")

        # Vrstva 2: ControlField / getTextWithFields fallback
        # Ověř zda jsme vůbec v seznamu podle fields
        fields_res = None
        if caret_info is not None:
            try:
                fields_res = _get_list_info_from_fields(caret_info)
            except Exception:
                fields_res = None
        if not fields_res or not fields_res.get("has_list"):
            return None, None

        # Synthetic list_id – zkus runtimeID, jinak segment-aware id
        rid = None
        try:
            fields = _get_fields_list(caret_info)
            if fields:
                for it in fields:
                    f = getattr(it, "field", None)
                    if f is None and isinstance(it, dict):
                        f = it
                    if f and f.get("role") == controlTypes.ROLE_LIST:
                        rid = f.get("runtimeID") or f.get("uniqueID")
                        if rid:
                            break
        except:
            rid = None
        if rid is not None:
            list_id = rid
        else:
            # segment-aware fallback pro více seznamů se stejným total
            try:
                all_fields = None
                try:
                    all_fields = _get_fields_list(ti.makeTextInfo(textInfos.POSITION_ALL), force_report_lists=True) if ti else None
                except:
                    all_fields = None
                seg_id = _get_field_segment_id(ti, caret_info, all_fields)
                # kombinuj seg_id s max_depth aby se odlišilo vnoření
                list_id = f"{seg_id}:{fields_res.get('max_depth')}:{fields_res.get('list_count')}"
            except:
                list_id = f"field:{ti_id}:{fields_res.get('list_count')}:{fields_res.get('max_depth')}"

        if list_id in cls._cache:
            return cls._cache[list_id], list_id

        # Spočítej total přes ControlField scan celého dokumentu (segment-aware)
        total = _get_total_levels_from_fields(ti, caret_info)
        if total is None or total < 1:
            # fallback na max_depth z caret vicinity
            total = fields_res.get("max_depth") or fields_res.get("list_count")
        if total is None or total < 1:
            return None, list_id
        if total > 20:
            total = 20
        cls._cache[list_id] = total
        return total, list_id

    @classmethod
    def check_and_speak(cls, obj, ti=None):
        # Respektuj nastavení
        try:
            enabled = config.conf["listLevelAddon"].get("announceBrowseModeTotalLevels", False)
        except:
            enabled = False
        if not enabled:
            return
        # Respektuj vypnutý režim 'nothing' – celkově nic nehlásit
        try:
            mode = config.conf["listLevelAddon"].get("mode", "everything")
            if mode == "nothing":
                return
        except:
            pass

        is_browse, ti = _is_browse_mode(obj, ti)
        if not is_browse or ti is None:
            return

        # Získej caret objekt
        caret_obj, caret_info = _get_caret_object(ti, obj)

        # Rychlá kontrola, zda jsme vůbec v seznamu (vrstva 1: ancestors, vrstva 2: ControlField)
        outermost_check = _find_outermost_list(caret_obj)
        if outermost_check is None:
            # Vrstva 2 – zkus ControlField
            try:
                fields_res = _get_list_info_from_fields(caret_info) if caret_info else None
                if not fields_res or not fields_res.get("has_list"):
                    if cls._last_browse_list_id is not None:
                        cls._last_browse_list_id = None
                        cls._last_browse_obj_id = None
                    return
                # jsme v seznamu podle fields → pokračuj, outermost_check zůstane None ale _get_total_levels to zpracuje
            except Exception:
                if cls._last_browse_list_id is not None:
                    cls._last_browse_list_id = None
                    cls._last_browse_obj_id = None
                return

        # Deduplikace na úrovni objektu (rychlé šipkování tam a zpět)
        try:
            obj_id = (getattr(caret_obj, 'IAccessibleObject', None), getattr(caret_obj, 'UIAElement', None))
            if obj_id == cls._last_browse_obj_id:
                return
            cls._last_browse_obj_id = obj_id
        except:
            pass

        total, list_id = cls._get_total_levels(ti, caret_obj, caret_info)
        if total is None or list_id is None:
            return

        # Hlášení pouze při vstupu do seznamu (změna outermost list)
        if list_id == cls._last_browse_list_id:
            return
        cls._last_browse_list_id = list_id

        try:
            if not config.conf["documentFormatting"]["reportLists"]:
                logHandler.log.debug("ListLevelAddon: reportLists disabled, still announcing total levels per user setting")
        except:
            pass

        levels_text = _get_cz_levels_text(total)
        msg = _("Seznam, {levels}.").format(levels=levels_text)
        try:
            speech.speakMessage(msg)
            logHandler.log.info(f"ListLevelAddon BrowseMode: oznámeno {msg} (list_id={list_id}, total={total})")
        except Exception as e:
            logHandler.log.error(f"ListLevelAddon BrowseMode speak failed: {e}")

    @classmethod
    def reset_cache(cls):
        cls._cache.clear()
        cls._cache_ti_id = None
        cls._last_browse_list_id = None
        cls._last_browse_obj_id = None

class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    __gestures = {
        "kb:nvda+alt+l": "toggleMode",
        "kb:nvda+shift+alt+l": "openSettings",
    }

    def __init__(self):
        super(GlobalPlugin, self).__init__()
        addonHandler.initTranslation()
        logHandler.log.info("ListLevelAddon: Inicializace doplňku")
        config.conf.spec["listLevelAddon"] = config.ConfigObj(
            io.StringIO(confspec), encoding="utf-8", list_values=False, interpolation=False, default_encoding="utf-8"
        )
        
        try:
            if ListLevelSettingsPanel not in NVDASettingsDialog.categoryClasses:
                NVDASettingsDialog.categoryClasses.append(ListLevelSettingsPanel)
        except Exception as e:
            logHandler.log.error(f"ListLevelAddon: Chyba při přidávání panelu: {e}")

    def terminate(self):
        try:
            if ListLevelSettingsPanel in NVDASettingsDialog.categoryClasses:
                NVDASettingsDialog.categoryClasses.remove(ListLevelSettingsPanel)
        except:
            pass
        BrowseModeListDetector.reset_cache()

    def event_gainFocus(self, obj, nextHandler):
        nextHandler()
        # V Browse Mode deleguj na caret (zabrání duplicitě)
        try:
            is_browse, _ = _is_browse_mode(obj)
            if is_browse:
                return
        except:
            pass
        ListLevelLogic.check_and_speak(obj)

    def event_caret(self, obj, nextHandler):
        nextHandler()
        # Rozlišení Browse Mode vs objektová hierarchie
        try:
            is_browse, ti = _is_browse_mode(obj)
        except:
            is_browse, ti = False, None

        if is_browse:
            # Browse Mode: nejdříve celkový počet úrovní (při vstupu), poté standardní úroveň/pozice
            try:
                BrowseModeListDetector.check_and_speak(obj, ti)
            except Exception as e:
                logHandler.log.error(f"ListLevelAddon event_caret BrowseMode error: {e}")
            try:
                caret_obj, _ = _get_caret_object(ti, obj)
                ListLevelLogic.check_and_speak(caret_obj)
            except:
                ListLevelLogic.check_and_speak(obj)
        else:
            try:
                ListLevelLogic.check_and_speak(obj)
            except Exception as e:
                logHandler.log.error(f"ListLevelAddon event_caret object error: {e}")

    def event_treeInterceptorChanged(self, obj, nextHandler):
        # Invalidace cache při změně dokumentu / přepnutí Browse/Focus
        nextHandler()
        try:
            BrowseModeListDetector.reset_cache()
            # Reset i objektové deduplikace aby se znovu ohlásilo po přepnutí
            ListLevelLogic._last_obj_id = None
            ListLevelLogic._last_list_id = None
            ListLevelLogic._last_level = None
        except:
            pass

    def event_documentLoadComplete(self, obj, nextHandler):
        nextHandler()
        try:
            BrowseModeListDetector.reset_cache()
        except:
            pass

    def script_toggleMode(self, gesture):
        modes = ['nothing', 'lists_only', 'levels_only', 'position_only', 'everything']
        try:
            current_mode = config.conf["listLevelAddon"].get("mode", "everything")
        except:
            current_mode = "everything"
            
        new_index = (modes.index(current_mode) + 1) % len(modes)
        new_mode = modes[new_index]
        config.conf["listLevelAddon"]["mode"] = new_mode
        
        mode_names = {
            'nothing': _("Nic"),
            'lists_only': _("Pouze seznamy"),
            'levels_only': _("Pouze úrovně"),
            'position_only': _("Jen pozice"),
            'everything': _("Všechno")
        }
        speech.speakMessage(_("Režim seznamů: {mode}").format(mode=mode_names[new_mode]))
    
    script_toggleMode.__doc__ = _("Přepíná mezi režimy oznamování informací o seznamech.")

    def script_openSettings(self, gesture):
        gui.settingsDialogs.NVDASettingsDialog(gui.mainFrame, ListLevelSettingsPanel).Show()
    
    script_openSettings.__doc__ = _("Otevře dialog nastavení doplňku List Level Addon.")
