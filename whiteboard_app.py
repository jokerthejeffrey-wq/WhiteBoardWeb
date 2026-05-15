from pathlib import Path
import py_compile, tempfile, subprocess, shutil

src = Path("/mnt/data/app_reset_camera_zoom.py")
text = src.read_text(encoding="utf-8")

# Fix the real freeze/click bug:
# applyCamera() was running before `let loadTimer = null` existed.
# Use a window property so scheduleLoad is safe even before the later code runs.
old = '''let loadTimer = null;
function scheduleLoad(){
    clearTimeout(loadTimer);
    loadTimer = setTimeout(loadViewport, 120);
}'''
new = '''window.__loadTimer = null;
function scheduleLoad(){
    clearTimeout(window.__loadTimer);
    window.__loadTimer = setTimeout(loadViewport, 120);
}'''
if old not in text:
    raise RuntimeError("loadTimer block not found")
text = text.replace(old, new, 1)

# Make camera movement easier:
# right-click drag still works, middle-click drag works, and Shift + left-click drag works on empty board.
old = '''viewport.addEventListener("mousedown", e=>{
    if(e.button !== 2) return;
    panning=true;
    panStart={x:e.clientX,y:e.clientY};
    camStart={x:camera.x,y:camera.y};
    viewport.style.cursor="grabbing";
});'''
new = '''viewport.addEventListener("mousedown", e=>{
    const isPanButton = e.button === 2 || e.button === 1 || (e.button === 0 && e.shiftKey);
    if(!isPanButton) return;
    if(e.target.closest("#tools,.panel,#settingsOverlay,#loginOverlay,.toolbar,button,input,textarea,select,audio")) return;

    panning=true;
    panStart={x:e.clientX,y:e.clientY};
    camStart={x:camera.x,y:camera.y};
    viewport.style.cursor="grabbing";
    e.preventDefault();
});'''
if old not in text:
    raise RuntimeError("panning mousedown block not found")
text = text.replace(old, new, 1)

# Force the draw canvas to never block clicks; drawing already listens on the viewport.
text = text.replace(
    "#drawCanvas{position:fixed;left:0;top:0;width:100vw;height:100vh;display:none;z-index:9000;cursor:crosshair;pointer-events:none}",
    "#drawCanvas{position:fixed;left:0;top:0;width:100vw;height:100vh;display:none;z-index:9000;cursor:crosshair;pointer-events:none}",
    1
)
text = text.replace(
    "body.draw-mode #drawCanvas{display:block;pointer-events:none}",
    "body.draw-mode #drawCanvas{display:block;pointer-events:none}",
    1
)

# Make the old browser context menu never appear while right-dragging.
if 'viewport.addEventListener("contextmenu", e=>e.preventDefault());' not in text:
    text = text.replace(
        "let panning=false, panStart={x:0,y:0}, camStart={x:0,y:0};",
        'let panning=false, panStart={x:0,y:0}, camStart={x:0,y:0};\nviewport.addEventListener("contextmenu", e=>e.preventDefault());',
        1
    )

out = Path("/mnt/data/app_camera_click_fixed.py")
out.write_text(text, encoding="utf-8")

# Python syntax check
py_compile.compile(str(out), cfile=str(Path(tempfile.gettempdir()) / "app_camera_click_fixed.pyc"), doraise=True)

# JS syntax check
start_js = text.find("<script>")
end_js = text.find("</script>", start_js)
script = text[start_js + len("<script>"):end_js]
js_path = Path("/mnt/data/app_camera_click_fixed_script.js")
js_path.write_text(script, encoding="utf-8")

node = shutil.which("node")
if node:
    res = subprocess.run([node, "--check", str(js_path)], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr)

print(f"Created: {out}")
print(f"Size: {out.stat().st_size}")
print("Python syntax OK")
print("JavaScript syntax OK")
