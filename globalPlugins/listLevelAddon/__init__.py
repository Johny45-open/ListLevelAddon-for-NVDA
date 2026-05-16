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

# Inicializace překladů pro doplněk
_ = gettext.gettext

# Definice konfiguračního schématu
confspec = """
[listLevelAddon]
    mode = string(default='everything')
"""

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

    def onSave(self):
        selection = self.mode_choice.GetSelection()
        if selection != wx.NOT_FOUND:
            config.conf["listLevelAddon"]["mode"] = self.choices[selection][0]

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

        in_list = False
        level = None
        item_count = None
        current_index = None
        list_count = 0
        list_id = None
        
        temp = obj
        depth = 0
        MAX_DEPTH = 10 
        
        while temp and depth < MAX_DEPTH:
            if temp.role == controlTypes.ROLE_LIST:
                in_list = True
                list_count += 1
                if list_id is None:
                    list_id = getattr(temp, 'IAccessibleObject', id(temp))
            
            if temp.role == controlTypes.ROLE_LISTITEM:
                in_list = True
            
            if level is None: level = getattr(temp, 'level', None)
            if item_count is None: item_count = getattr(temp, 'itemCount', None)
            if current_index is None: current_index = getattr(temp, 'indexInGroup', None)
            
            pos = getattr(temp, 'positionInfo', None)
            if pos:
                if level is None: level = pos.get('level')
                if item_count is None: item_count = pos.get('similarItemsInGroup')
                if current_index is None: current_index = pos.get('indexInGroup')
            
            temp = temp.parent
            depth += 1

        if not in_list:
            return None, None, None, None, None

        list_msgs = []
        if mode in ('lists_only', 'everything'):
            list_msgs.append(_("Seznam, zanoření {count}").format(count=list_count))
        
        level_msg = None
        if mode in ('levels_only', 'everything'):
            display_level = level if level is not None else list_count
            level_msg = _("úroveň {level}").format(level=display_level)

        pos_msg = None
        if mode in ('position_only', 'everything'):
            if current_index and item_count:
                pos_msg = _("{current} z {total} položek").format(current=current_index, total=item_count)
            elif mode == 'position_only' and item_count:
                pos_msg = _("{count} položek").format(count=item_count)

        return ", ".join(list_msgs), level_msg, pos_msg, list_id, level

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

class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    __gestures = {
        "kb:nvda+alt+l": "toggleMode",
        "kb:nvda+shift+alt+l": "openSettings",
    }

    def __init__(self):
        super(GlobalPlugin, self).__init__()
        addonHandler.initTranslation()
        logHandler.log.info("ListLevelAddon: Inicializace doplňku")
        config.conf.spec["listLevelAddon"] = config.ConfigObj(confspec.splitlines(), encoding="utf-8", interpolation=False)
        
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

    def event_gainFocus(self, obj, nextHandler):
        nextHandler()
        ListLevelLogic.check_and_speak(obj)

    def event_caret(self, obj, nextHandler):
        nextHandler()
        browser_names = ('firefox', 'chrome', 'msedge', 'browser')
        app_name = getattr(obj.appModule, 'appName', '').lower()
        if app_name in browser_names:
            ListLevelLogic.check_and_speak(obj)

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
