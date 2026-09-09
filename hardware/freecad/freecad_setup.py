#!/usr/bin/env python3
"""
freecad_setup.py: reproducible FreeCAD 1.1 user environment (addons + prefs).

Installs a curated addon set into the FreeCAD user Mod directory and applies a
fixed preference set through freecadcmd. Standard library only.

Default mode is a dry run: nothing is cloned, pulled, or written. Pass
--install to perform the actions. Cloning from GitHub is an external fetch and
is therefore opt-in.

Modes
  (default)      dry run: print the addon and preference actions, change nothing
  --install      clone or fast-forward the addons, then apply the preferences
  --addons-only  restrict --install / dry run to the addon step
  --prefs-only   restrict --install / dry run to the preference step
  --check        run freecadcmd once per installed addon and import its
                 Init.py / InitGui.py; report load result per addon
  --with-optional  also handle addons whose verdict is "optional"

Examples
  python3 freecad_setup.py                      # dry run, full plan
  python3 freecad_setup.py --install            # addons + preferences
  python3 freecad_setup.py --install --prefs-only --user-cfg /tmp/test.cfg
  python3 freecad_setup.py --check

Paths (macOS, FreeCAD 1.1.3 Homebrew cask)
  freecadcmd  /Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd
  Mod dir     FreeCAD.getUserAppDataDir() + "Mod"
              = ~/Library/Application Support/FreeCAD/v1-1/Mod
  user.cfg    FreeCAD.getUserConfigDir() + "user.cfg"
              = ~/Library/Preferences/FreeCAD/v1-1/user.cfg
  Both directories are read from FreeCAD itself at runtime, never hard-coded,
  so the script follows whatever build is at --freecadcmd.

Preference keys applied (all verified, see source column)
  BaseApp/Preferences/Units            UserSchema   Int   0
      0 = "Standard (mm, kg, s, degree)". src/App/Application.cpp and
      src/Gui/PreferencePages/DlgSettingsGeneral.cpp at tag 1.1.3; key also
      present in a GUI-written user.cfg.
  BaseApp/Preferences/Units            Decimals     Int   3
      src/Gui/Application.cpp, QuantitySpinBox.cpp at 1.1.3. Runtime check:
      Quantity("1.23456789 mm").UserString prints 1.235 mm after the change.
  BaseApp/Preferences/General          AutoloadModule Text PartDesignWorkbench
      Key and value taken from a GUI-written user.cfg of 1.1.3.
  BaseApp/Preferences/View             NavigationStyle Text Gui::TinkerCADNavigationStyle
      Key from a GUI-written user.cfg; class name string present in
      libFreeCADGui.dylib. Button mapping from
      src/Gui/Navigation/TinkerCADNavigationStyle.cpp at 1.1.3: BUTTON1 (left)
      selection, BUTTON2 (right) DRAGGING (rotate), BUTTON3 (middle) PANNING,
      wheel zoom. This matches Onshape.
  BaseApp/Preferences/Mod/Sketcher/General AutoConstraints Bool 1
      src/Mod/Sketcher/Gui/ViewProviderSketch.cpp at 1.1.3: the
      ParameterObserver reads "AutoConstraints" from Mod/Sketcher/General
      (updateBoolProperty, default true) into the sketch view provider.
  BaseApp/Preferences/Mod/TechDraw/Files TemplateFile Text <resource dir>/Mod/TechDraw/Templates/ISO/A3_Landscape_ISO5457_advanced.svg
      src/Mod/TechDraw/App/Preferences.cpp at 1.1.3: defaultTemplate() reads
      getPreferenceGroup("Files")->GetASCII("TemplateFile"), full path; the
      group base is BaseApp/Preferences/Mod/TechDraw. The file exists in the
      bundle under share/Mod/TechDraw/Templates/ISO/. Override with
      --techdraw-template.
  Dark theme: the bundled preference pack
      <resource dir>/Gui/PreferencePacks/FreeCAD Dark/FreeCAD Dark.cfg is
      parsed and every value in it is applied. Its text keys are
      MainWindow/Theme = "FreeCAD Dark", MainWindow/StyleSheet = "FreeCAD.qss",
      MainWindow/OverlayActiveStyleSheet = "Freecad Overlay.qss",
      MainWindow/QtStyle = "FreeCAD", plus the pack's colour and view values.
      This is the same data the GUI "Apply preference pack" action merges.

Not applied / not verified
  TechDraw TemplateDir is left untouched; TemplateFile is a full path so the
  directory key is not needed. No other candidate key was left out.

Conventions: deterministic, explicit paths, idempotent, external side effects
behind --install. No third-party Python dependencies.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

DEFAULT_FREECADCMD = "/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd"
DEFAULT_THEME_PACK = "FreeCAD Dark"
DEFAULT_TECHDRAW_TEMPLATE = "Mod/TechDraw/Templates/ISO/A3_Landscape_ISO5457_advanced.svg"

# name = directory under Mod/, url = clone source, verdict = install | optional.
# Assessment (last commit dates, package.xml constraints) lives in the
# accompanying freecad_addons_assessment.md; keep this list in sync with it.
ADDONS = [
    {"name": "kicadStepUpMod", "url": "https://github.com/easyw/kicadStepUpMod",
     "verdict": "install", "purpose": "KiCad to FreeCAD ECAD/MCAD bridge"},
    {"name": "FreeCAD_FastenersWB", "url": "https://github.com/shaise/FreeCAD_FastenersWB",
     "verdict": "install", "purpose": "ISO/DIN screws, nuts, standoffs, press nuts"},
    {"name": "Defeaturing_WB", "url": "https://github.com/easyw/Defeaturing_WB",
     "verdict": "install", "purpose": "simplify vendor STEP (remove faces/holes)"},
    {"name": "Manipulator", "url": "https://github.com/easyw/Manipulator",
     "verdict": "install", "purpose": "move/align/measure imported parts"},
    {"name": "HistoryWorkbench", "url": "https://github.com/eblanshey/HistoryWorkbench",
     "verdict": "install", "purpose": "model history, 3D and tree diff for git review"},
    {"name": "CurvesWB", "url": "https://github.com/tomate44/CurvesWB",
     "verdict": "install", "purpose": "curve and surface tools beyond Part"},
    {"name": "MeshRemodel", "url": "https://github.com/mwganson/MeshRemodel",
     "verdict": "optional", "purpose": "rebuild solids from STL-only vendor meshes"},
]

PARAM_ROOT = "User parameter:"

# Inline FreeCAD script templates. They are formatted with a JSON payload and
# run through freecadcmd. Only the FreeCAD module and the standard library
# are used inside them.
PREFS_SCRIPT = r'''
import json, sys
import xml.etree.ElementTree as ET
import FreeCAD
cfg = json.loads(%(payload)s)
applied = []
def grp(path):
    return FreeCAD.ParamGet("User parameter:" + path)
setters = {
    "FCText": lambda g, n, v: g.SetString(n, v),
    "FCBool": lambda g, n, v: g.SetBool(n, v in ("1", "true", "True")),
    "FCInt": lambda g, n, v: g.SetInt(n, int(v)),
    "FCUInt": lambda g, n, v: g.SetUnsigned(n, int(v)),
    "FCFloat": lambda g, n, v: g.SetFloat(n, float(v)),
}
for path, name, kind, value in cfg["params"]:
    setters[kind](grp(path), name, value)
    applied.append((path, name, kind, value))
pack = cfg.get("pack")
pack_count = 0
if pack:
    tree = ET.parse(pack)
    def walk(node, path):
        global pack_count
        for child in node:
            tag = child.tag
            if tag == "FCParamGroup":
                sub = child.get("Name")
                walk(child, path + [sub])
            elif tag in setters:
                p = "/".join(path)
                v = child.get("Value") if child.get("Value") is not None else (child.text or "")
                setters[tag](grp(p), child.get("Name"), v)
                pack_count += 1
    root = tree.getroot()
    for top in root:
        if top.tag == "FCParamGroup" and top.get("Name") == "Root":
            walk(top, [])
FreeCAD.saveParameter()
print("PREFS_OK", json.dumps({"applied": applied, "pack_values": pack_count,
      "user_cfg": FreeCAD.getUserConfigDir() + "user.cfg"}))
'''

INFO_SCRIPT = r'''
import json, FreeCAD
print("INFO", json.dumps({
    "version": ".".join(FreeCAD.Version()[:3]),
    "mod_dir": FreeCAD.getUserAppDataDir() + "Mod",
    "user_cfg": FreeCAD.getUserConfigDir() + "user.cfg",
    "resource_dir": FreeCAD.getResourceDir(),
}))
'''

CHECK_SCRIPT = r'''
import glob, importlib, json, os, sys, traceback
import FreeCAD
addon = %(payload)s
sys.path.insert(0, addon)
# Two addon layouts exist. Classic: Init.py (headless) + InitGui.py (GUI).
# Namespace package: freecad/<pkg>/__init__.py (headless) + init_gui.py (GUI).
# FreeCAD runs Init.py as exec(compile(src, path, "exec")) inside FreeCADInit's
# globals, where both FreeCAD and App are bound (src/App/FreeCADInit.py at
# 1.1.3); the same globals are used here so the check matches the real loader.
def last_line(e):
    return "".join(traceback.format_exception_only(type(e), e)).strip().splitlines()[-1][:160]
def exec_file(path):
    g = {"__name__": "__main__", "__file__": path, "FreeCAD": FreeCAD, "App": FreeCAD}
    with open(path, "rt", encoding="utf-8") as f:
        exec(compile(f.read(), path, "exec"), g)
result = {"layout": None, "headless": None, "gui": None}
init_py = os.path.join(addon, "Init.py")
initgui_py = os.path.join(addon, "InitGui.py")
pkgs = [p for p in glob.glob(os.path.join(addon, "freecad", "*", "__init__.py"))]
if os.path.exists(init_py) or os.path.exists(initgui_py):
    result["layout"] = "classic"
    if os.path.exists(init_py):
        try:
            exec_file(init_py); result["headless"] = "Init.py ok"
        except BaseException as e:
            result["headless"] = "Init.py error: " + last_line(e)
    else:
        result["headless"] = "no Init.py"
    if os.path.exists(initgui_py):
        try:
            exec_file(initgui_py); result["gui"] = "InitGui.py ok (unexpected headless)"
        except BaseException as e:
            result["gui"] = "InitGui.py GUI-only, not verifiable headless (" + last_line(e) + ")"
    else:
        result["gui"] = "no InitGui.py"
elif pkgs:
    result["layout"] = "namespace"
    heads, guis = [], []
    for p in pkgs:
        name = "freecad." + os.path.basename(os.path.dirname(p))
        try:
            importlib.import_module(name); heads.append(name + " ok")
        except BaseException as e:
            heads.append(name + " error: " + last_line(e))
        if os.path.exists(os.path.join(os.path.dirname(p), "init_gui.py")):
            try:
                importlib.import_module(name + ".init_gui"); guis.append(name + ".init_gui ok (unexpected headless)")
            except BaseException as e:
                guis.append(name + ".init_gui GUI-only, not verifiable headless (" + last_line(e) + ")")
        else:
            guis.append(name + " has no init_gui.py")
    result["headless"] = "; ".join(heads)
    result["gui"] = "; ".join(guis)
else:
    result["layout"] = "unknown"
    result["headless"] = "no Init.py and no freecad/<pkg>/__init__.py found"
    result["gui"] = ""
print("CHECK", json.dumps(result))
'''


def log(msg):
    print(msg, flush=True)


def run_freecadcmd(freecadcmd, code, user_cfg=None, extra_env=None):
    """Run inline Python inside freecadcmd. Returns (returncode, stdout, stderr).

    freecadcmd rewrites its user.cfg on exit. Read-only runs (path query,
    load check) therefore always get a throwaway --user-cfg so the live
    configuration is never touched unless the caller passes its own path.
    """
    cmd = [freecadcmd]
    with tempfile.TemporaryDirectory(prefix="freecad_setup_") as tmp:
        cmd += ["--user-cfg", user_cfg or os.path.join(tmp, "throwaway_user.cfg")]
        script = os.path.join(tmp, "run.py")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(code)
        cmd.append(script)
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def parse_marker(stdout, marker):
    for line in stdout.splitlines():
        if line.startswith(marker + " "):
            return json.loads(line[len(marker) + 1:])
    return None


def freecad_info(freecadcmd, user_cfg=None):
    rc, out, err = run_freecadcmd(freecadcmd, INFO_SCRIPT, user_cfg)
    info = parse_marker(out, "INFO")
    if info is None:
        sys.exit("freecadcmd did not report its paths (rc=%d):\n%s\n%s" % (rc, out, err))
    return info


def gui_running():
    """True if a FreeCAD GUI process is running. It rewrites user.cfg on exit
    and would overwrite preferences applied here."""
    if shutil.which("pgrep") is None:
        return False
    proc = subprocess.run(["pgrep", "-x", "FreeCAD"], capture_output=True, text=True)
    return proc.returncode == 0


def git(args, cwd=None):
    proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def selected_addons(with_optional):
    return [a for a in ADDONS if a["verdict"] == "install" or with_optional]


def addon_state(mod_dir, addon):
    dest = os.path.join(mod_dir, addon["name"])
    if not os.path.exists(dest):
        return "absent", dest
    if not os.path.isdir(os.path.join(dest, ".git")):
        return "present-not-git", dest
    rc, remote = git(["-C", dest, "remote", "get-url", "origin"])
    if rc != 0:
        return "present-no-origin", dest
    if remote.rstrip("/").removesuffix(".git") != addon["url"].rstrip("/").removesuffix(".git"):
        return "present-other-origin", dest
    return "present", dest


def do_addons(mod_dir, addons, install):
    """Clone or fast-forward each addon. Returns list of (name, action, detail)."""
    results = []
    if not os.path.isdir(mod_dir):
        if install:
            os.makedirs(mod_dir, exist_ok=True)
            log("created %s" % mod_dir)
        else:
            log("[dry-run] would create %s" % mod_dir)
    for addon in addons:
        state, dest = addon_state(mod_dir, addon)
        if state == "absent":
            if install:
                rc, out = git(["clone", "--depth", "1", addon["url"], dest])
                results.append((addon["name"], "cloned" if rc == 0 else "clone-failed", out if rc else ""))
                log("%-22s %s" % (addon["name"], "cloned" if rc == 0 else "clone FAILED: " + out))
            else:
                results.append((addon["name"], "would-clone", dest))
                log("[dry-run] %-22s would clone %s -> %s" % (addon["name"], addon["url"], dest))
        elif state == "present":
            if install:
                rc, out = git(["-C", dest, "pull", "--ff-only", "--depth", "1"])
                action = "updated" if rc == 0 else "pull-failed"
                results.append((addon["name"], action, out if rc else ""))
                log("%-22s %s" % (addon["name"], "pulled (ff-only)" if rc == 0 else "pull FAILED: " + out))
            else:
                results.append((addon["name"], "would-pull", dest))
                log("[dry-run] %-22s present, would git pull --ff-only" % addon["name"])
        else:
            results.append((addon["name"], "skipped-" + state, dest))
            log("%-22s SKIPPED: %s at %s (not touching it)" % (addon["name"], state, dest))
    for addon in addons:
        state, dest = addon_state(mod_dir, addon)
        if state == "present":
            rc, head = git(["-C", dest, "log", "-1", "--format=%h %cs"])
            if rc == 0:
                log("%-22s HEAD %s" % (addon["name"], head))
    return results


def build_params(info, args):
    """Preference list as (group path, name, kind, value) tuples."""
    template = args.techdraw_template
    if not os.path.isabs(template):
        template = os.path.join(info["resource_dir"], template)
    params = [
        ("BaseApp/Preferences/Units", "UserSchema", "FCInt", "0"),
        ("BaseApp/Preferences/Units", "Decimals", "FCInt", "3"),
        ("BaseApp/Preferences/General", "AutoloadModule", "FCText", "PartDesignWorkbench"),
        ("BaseApp/Preferences/View", "NavigationStyle", "FCText", "Gui::TinkerCADNavigationStyle"),
        ("BaseApp/Preferences/Mod/Sketcher/General", "AutoConstraints", "FCBool", "1"),
        ("BaseApp/Preferences/Mod/TechDraw/Files", "TemplateFile", "FCText", template),
    ]
    pack = None
    if args.theme_pack:
        pack = os.path.join(info["resource_dir"], "Gui", "PreferencePacks",
                            args.theme_pack, args.theme_pack + ".cfg")
    return params, pack, template


def do_prefs(freecadcmd, info, args, install):
    params, pack, template = build_params(info, args)
    problems = []
    if not os.path.exists(template):
        problems.append("TechDraw template not found: %s" % template)
    if pack and not os.path.exists(pack):
        problems.append("theme pack not found: %s" % pack)
    for p in problems:
        log("ERROR " + p)
    if problems:
        return False, params, pack
    log("user.cfg: %s" % (args.user_cfg or info["user_cfg"]))
    for path, name, kind, value in params:
        log("%s %s/%s = %s" % ("[dry-run]" if not install else "set", path, name, value))
    if pack:
        log("%s apply preference pack %s" % ("[dry-run]" if not install else "set", pack))
    if not install:
        return True, params, pack
    if gui_running():
        log("WARNING: a FreeCAD GUI process is running; it rewrites user.cfg on exit "
            "and will overwrite these values. Quit FreeCAD and rerun.")
    target_cfg = args.user_cfg or info["user_cfg"]
    payload = json.dumps(json.dumps({"params": params, "pack": pack}))
    rc, out, err = run_freecadcmd(freecadcmd, PREFS_SCRIPT % {"payload": payload}, target_cfg)
    res = parse_marker(out, "PREFS_OK")
    if res is None:
        log("preference apply FAILED (rc=%d)\n%s\n%s" % (rc, out, err))
        return False, params, pack
    log("applied %d explicit values and %d pack values to %s"
        % (len(res["applied"]), res["pack_values"], target_cfg))
    return True, params, pack


def do_check(freecadcmd, mod_dir, addons, user_cfg):
    """Import each addon's Init.py and InitGui.py inside freecadcmd."""
    rows = []
    for addon in addons:
        dest = os.path.join(mod_dir, addon["name"])
        if not os.path.isdir(dest):
            rows.append((addon["name"], "not installed", ""))
            continue
        rc, out, err = run_freecadcmd(freecadcmd, CHECK_SCRIPT % {"payload": json.dumps(dest)}, user_cfg)
        res = parse_marker(out, "CHECK")
        if res is None:
            last = ((err or out).strip().splitlines() or [""])[-1][:160]
            rows.append((addon["name"], "freecadcmd crashed", last))
            continue
        rows.append((addon["name"], res["headless"], "[%s] %s" % (res["layout"], res["gui"])))
    log("")
    log("Load check (headless freecadcmd). GUI entry points cannot load without FreeCADGui;")
    log("they are reported as GUI-only, not as failures.")
    for name, a, b in rows:
        log("  %-22s %s" % (name, a))
        log("  %-22s %s" % ("", b))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install", action="store_true",
                    help="perform the actions (clone/pull addons, write preferences); default is dry run")
    ap.add_argument("--dry-run", action="store_true", help="print actions only (the default)")
    ap.add_argument("--addons-only", action="store_true", help="only the addon step")
    ap.add_argument("--prefs-only", action="store_true", help="only the preference step")
    ap.add_argument("--check", action="store_true",
                    help="import each installed addon's Init modules in freecadcmd and report")
    ap.add_argument("--with-optional", action="store_true", help="include addons with verdict 'optional'")
    ap.add_argument("--freecadcmd", default=DEFAULT_FREECADCMD, help="path to freecadcmd")
    ap.add_argument("--mod-dir", default=None, help="override the Mod directory (default: from FreeCAD)")
    ap.add_argument("--user-cfg", default=None,
                    help="alternate user.cfg passed to freecadcmd --user-cfg (testing; default: FreeCAD's own)")
    ap.add_argument("--theme-pack", default=DEFAULT_THEME_PACK,
                    help="bundled preference pack name to apply; empty string to skip (default: %(default)s)")
    ap.add_argument("--techdraw-template", default=DEFAULT_TECHDRAW_TEMPLATE,
                    help="TechDraw template, absolute or relative to the FreeCAD resource dir")
    args = ap.parse_args()

    if args.addons_only and args.prefs_only:
        ap.error("--addons-only and --prefs-only are mutually exclusive")
    if args.install and args.dry_run:
        ap.error("--install and --dry-run are mutually exclusive")
    install = args.install

    if not os.path.exists(args.freecadcmd):
        sys.exit("freecadcmd not found: %s" % args.freecadcmd)
    if shutil.which("git") is None:
        sys.exit("git not found on PATH")

    info = freecad_info(args.freecadcmd, args.user_cfg)
    mod_dir = args.mod_dir or info["mod_dir"]
    addons = selected_addons(args.with_optional)
    log("FreeCAD %s  Mod dir: %s  mode: %s" % (info["version"], mod_dir, "install" if install else "dry-run"))

    addon_results, prefs_ok, check_rows = [], None, []
    if args.check:
        check_rows = do_check(args.freecadcmd, mod_dir, addons, args.user_cfg)
    else:
        if not args.prefs_only:
            log("")
            log("Addons (%d selected%s):" % (len(addons), "" if args.with_optional else ", optional excluded"))
            addon_results = do_addons(mod_dir, addons, install)
        if not args.addons_only:
            log("")
            log("Preferences:")
            prefs_ok, _, _ = do_prefs(args.freecadcmd, info, args, install)

    log("")
    log("Summary")
    if addon_results:
        counts = {}
        for _, action, _ in addon_results:
            counts[action] = counts.get(action, 0) + 1
        log("  addons: " + ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    if prefs_ok is not None:
        log("  preferences: %s" % ("applied" if install and prefs_ok else "planned" if prefs_ok else "FAILED"))
    if check_rows:
        bad = [r for r in check_rows if " error" in r[1] or r[1] in ("not installed", "freecadcmd crashed")]
        log("  check: %d addons, %d with problems" % (len(check_rows), len(bad)))
    if not install and not args.check:
        log("  dry run; rerun with --install to apply")
    failed = any(a in ("clone-failed", "pull-failed") for _, a, _ in addon_results) or prefs_ok is False
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
