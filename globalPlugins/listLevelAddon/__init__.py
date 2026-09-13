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


def get_list_level(obj, ti=None):
    """Společná abstrakce pro aktuální úroveň seznamu.
    Automaticky překládá dokument -> caret_obj v Browse Mode.
    Priority:
      1) Browse Mode: caret_obj parent-chain count (robustní pro Gecko kde level neexistuje)
      2) Focus Mode: positionInfo['level'] / obj.level pokud je autoritativní int
      3) Fallback: parent ROLE_LIST count
    Vrací int (1-based) nebo None pokud nelze spolehlivě určit / není v seznamu.
    """
    # Překlad v Browse Mode
    try:
        is_browse, ti_resolved = _is_browse_mode(obj, ti)
        if is_browse and ti_resolved is not None:
            caret_obj, _ = _get_caret_object(ti_resolved, obj)
            # Použij caret_obj pokud se liší od dokumentu
            if caret_obj is not obj:
                obj = caret_obj
                ti = ti_resolved
    except:
        pass

    # Rychlá kontrola zda jsme v seznamu
    count, outermost = _count_list_ancestors(obj)
    if count == 0 or outermost is None:
        # Ještě zkontroluj zda samotný obj je LISTITEM uvnitř LISTu přes parent
        # _count_list_ancestors už pokrývá oba případy, takže 0 => mimo seznam
        return None

    # Zkus autoritativní level z positionInfo / level atributu (UIA, IA2 groupPosition)
    # Procházíme od obj nahoru a bereme první int level
    temp = obj
    depth = 0
    MAX_DEPTH = 20
    while temp is not None and depth < MAX_DEPTH:
        try:
            # 1) přímý atribut level
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
        try:
            is_browse, ti = _is_browse_mode(obj)
            if is_browse and ti is not None:
                caret_obj, _ = _get_caret_object(ti, obj)
                if caret_obj is not obj:
                    effective_obj = caret_obj
        except:
            effective_obj = obj

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
            return None, None, None, None, None

        # Robustní určení úrovně – společná abstrakce
        # get_list_level už řeší Browse vs Focus a preference positionInfo > ancestor count
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

    Používá NVDAObject strom (DFS) od nejvnějšího LISTu jako primární a jediný
    spolehlivý zdroj. Žádná heuristika podle celého dokumentu – pokud DFS
    neposkytne výsledek, raději mlčí (požadavek zadání).
    Cache: outermost list uniqueID -> totalLevels, platné pro daný treeInterceptor.
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
        if outermost is None:
            return None, None
        list_id = _get_list_unique_id(outermost)
        # Cache invalidace při změně dokumentu
        ti_id = id(ti)
        if cls._cache_ti_id != ti_id:
            cls._cache.clear()
            cls._cache_ti_id = ti_id
            cls._last_browse_list_id = None
        if list_id in cls._cache:
            return cls._cache[list_id], list_id
        # Spočítej DFS – jediný spolehlivý zdroj
        total = get_list_depth(outermost)
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

        # Rychlá kontrola, zda jsme vůbec v seznamu (role LIST/LISTITEM v ancestors)
        outermost_check = _find_outermost_list(caret_obj)
        if outermost_check is None:
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
