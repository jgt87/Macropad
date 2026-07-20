#Requires AutoHotkey v2.0
#SingleInstance Force
;===================================================================
;  Macropad Helper  -  companion app for the VID:1189 PID:8890 macropad
;-------------------------------------------------------------------
;  Bind ONE macropad key to send  Ctrl+Alt+Win+F12.  When you press it,
;  this app pops up an action menu at your cursor - turning a single key
;  into as many actions as you like, including things the macropad can't
;  do by itself (long text, slash commands, app launches, app-aware behavior).
;
;  Program the macropad key with:
;      python macropad.py key <N> ctrl+alt+win+f12
;
;  Runs in the system tray. Right-click the tray icon to Edit / Reload / Exit.
;  To customize: edit BuildActions() below - add or remove lines.
;===================================================================

A_IconTip := "Macropad Helper  (macropad key -> action menu)"

; ---- system tray menu ---------------------------------------------
tm := A_TrayMenu
tm.Delete()
tm.Add("Macropad Helper", (*) => "")
tm.Disable("Macropad Helper")
tm.Add()
tm.Add("Show action menu now", (*) => ShowActionMenu())
tm.Add("Edit this script", (*) => Run('notepad.exe "' A_ScriptFullPath '"'))
tm.Add("Reload", (*) => Reload())
tm.Add("Exit", (*) => ExitApp())

; ===================================================================
;  THE MACROPAD TRIGGER
;  Macropad key sends  Ctrl+Alt+Win+F12  =  ^ ! # F12
; ===================================================================
^!#F12::ShowActionMenu()

; ---- the pop-up action menu ---------------------------------------
ShowActionMenu() {
    m := Menu()
    BuildActions(m)
    m.Show()          ; appears at the mouse cursor
}

; ===================================================================
;  EDIT HERE - your actions. Each line: m.Add("Label", handler)
;  Handlers: Send(...) for keystrokes, SendCmd(...) to type + Enter,
;  Run(...) to launch, DllCall(...) for system actions.
; ===================================================================
BuildActions(m) {
    m.Add("Hard reload page  (Ctrl+F5)",     (*) => Send("^{F5}"))
    m.Add("Task View  (Win+Tab)",            (*) => Send("#{Tab}"))
    m.Add("Lock screen  (Win+L)",            (*) => DllCall("LockWorkStation"))
    m.Add()  ; separator
    m.Add("Claude: /compact + Enter",        (*) => SendCmd("/compact"))
    m.Add("Claude: /clear + Enter",          (*) => SendCmd("/clear"))
    m.Add("Claude: /cost + Enter",           (*) => SendCmd("/cost"))
    m.Add()
    m.Add("Type: my email",                  (*) => SendText("your.email@example.com"))
    m.Add("Open VS Code",                    (*) => Run("code"))
    m.Add()
    m.Add("Cancel",                          (*) => "")
}

; ---- helpers ------------------------------------------------------
; Type literal text (slash commands, snippets) then press Enter.
SendCmd(text) {
    SendText(text)
    Send("{Enter}")
}

; ===================================================================
;  OPTIONAL: app-aware DIRECT action (no menu).
;  To use this instead of the menu, comment out the "^!#F12::ShowActionMenu()"
;  line above and uncomment this block:
; ===================================================================
; ^!#F12:: {
;     exe := StrLower(WinGetProcessName("A"))
;     if InStr(exe, "chrome") || InStr(exe, "msedge") || InStr(exe, "firefox")
;         Send("^{F5}")            ; browser  -> hard reload
;     else if (exe = "code.exe")
;         Send("^+p")              ; VS Code   -> Command Palette
;     else
;         ShowActionMenu()         ; anything else -> the menu
; }
