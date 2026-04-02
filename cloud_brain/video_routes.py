# video_routes.py
# FastAPI router — place this file inside cloud_brain/
#
# Then add TWO lines to cloud_brain/main.py (see comment at the bottom of this file).
# Also add to the ROOT requirements.txt:
#   yt-dlp
#   imageio-ffmpeg

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
import threading, uuid, os, struct, glob, subprocess
import imageio_ffmpeg

router = APIRouter()

CACHE_DIR  = os.environ.get("VIDEO_CACHE_DIR", "/tmp/bronny_videos")
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
TARGET_FPS = 15

os.makedirs(CACHE_DIR, exist_ok=True)

# ── In-memory job store ───────────────────────────────────────────────────────
_jobs: dict  = {}
_jobs_lock   = threading.Lock()

# ── Single-slot queue: job_id the ESP32 should play next ─────────────────────
_current_job  = ""
_current_lock = threading.Lock()

def _update(job_id: str, status: str, error: str = None):
    with _jobs_lock:
        _jobs[job_id] = {"status": status}
        if error:
            _jobs[job_id]["error"] = error


# ── Background conversion worker ─────────────────────────────────────────────
def _convert(job_id: str, url: str):
    global _current_job
    raw_mp4    = os.path.join(CACHE_DIR, f"{job_id}_raw.mp4")
    temp_mjpeg = os.path.join(CACHE_DIR, f"{job_id}_tmp.mjpeg")
    out_mjpeg  = os.path.join(CACHE_DIR, f"{job_id}.mjpeg")
    out_mp3    = os.path.join(CACHE_DIR, f"{job_id}.mp3")

    try:
        # 1 — Download (360p max keeps conversion fast)
        _update(job_id, "downloading")
        r = subprocess.run([
            "yt-dlp",
            "-f", (
                "best[height<=360][ext=mp4]"
                "/best[height<=360]"
                "/bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]"
            ),
            "--ffmpeg-location", FFMPEG_EXE,
            "--merge-output-format", "mp4",
            "--no-playlist",
            "-o", raw_mp4,
            url,
        ], capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError("yt-dlp: " + r.stderr.decode(errors="replace")[-400:])
        if not os.path.exists(raw_mp4):
            candidates = glob.glob(os.path.join(CACHE_DIR, f"{job_id}_raw.*"))
            if not candidates:
                raise FileNotFoundError("yt-dlp produced no output file")
            raw_mp4 = candidates[0]

        # 2 — Video → MJPEG (320px wide, 25 fps, quality 7)
        _update(job_id, "converting")
        r = subprocess.run([
            FFMPEG_EXE, "-y", "-i", raw_mp4,
            "-vf", "scale=320:-2",
            "-c:v", "mjpeg", "-q:v", "10",
            "-r", str(TARGET_FPS), "-an", temp_mjpeg,
        ], capture_output=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg video: " + r.stderr.decode(errors="replace")[-400:])

        # Prepend 1-byte FPS header
        with open(temp_mjpeg, "rb") as fi, open(out_mjpeg, "wb") as fo:
            fo.write(struct.pack("B", TARGET_FPS))
            fo.write(fi.read())
        os.remove(temp_mjpeg)

        # 3 — Audio → MP3 (44100 Hz, mono, 96 kbps)
        r = subprocess.run([
            FFMPEG_EXE, "-y", "-i", raw_mp4,
            "-vn", "-ac", "1", "-ar", "44100",
            "-b:a", "96k", out_mp3,
        ], capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg audio: " + r.stderr.decode(errors="replace")[-400:])

        os.remove(raw_mp4)
        _update(job_id, "ready")

        with _current_lock:
            _current_job = job_id

        print(
            f"[Video] {job_id} ready — "
            f"mjpeg={os.path.getsize(out_mjpeg)//1024}KB  "
            f"mp3={os.path.getsize(out_mp3)//1024}KB"
        )

    except Exception as exc:
        _update(job_id, "error", str(exc)[:400])
        print(f"[Video] {job_id} FAILED: {exc}")
        for p in (raw_mp4, temp_mjpeg):
            if os.path.exists(p):
                try: os.remove(p)
                except: pass


# ── Embedded web UI ───────────────────────────────────────────────────────────
_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bronny Video</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#08080f;color:#d0d0e8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#0f0f1c;border:1px solid #1c1c38;border-radius:16px;padding:32px 26px;
      width:100%;max-width:460px;box-shadow:0 8px 40px rgba(0,200,255,.06)}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:26px}
.dot{width:9px;height:9px;border-radius:50%;background:#00e5ff;box-shadow:0 0 8px #00e5ff}
h1{font-size:1.15rem;color:#fff;letter-spacing:.06em}
.sub{font-size:.72rem;color:#44445a;margin-top:2px}
label{font-size:.78rem;color:#666;margin-bottom:6px;display:block}
input{width:100%;background:#09091a;border:1px solid #1e1e3c;border-radius:8px;
      color:#d0d0e8;font-size:.88rem;padding:12px 13px;outline:none;transition:border-color .2s}
input:focus{border-color:#00e5ff33}
button{width:100%;margin-top:12px;padding:13px;border:none;border-radius:8px;
       background:linear-gradient(135deg,#00b4d8,#0077b6);color:#fff;
       font-size:.93rem;font-weight:600;cursor:pointer;transition:opacity .2s}
button:disabled{opacity:.35;cursor:not-allowed}
#st{margin-top:18px;padding:12px 14px;border-radius:8px;font-size:.83rem;
    line-height:1.6;display:none}
.w{background:#14142a;border:1px solid #2a2a50;color:#666}
.p{background:#00112a;border:1px solid #003366;color:#00b4d8}
.r{background:#002418;border:1px solid #005030;color:#00e5a0}
.e{background:#240808;border:1px solid #501010;color:#ff5555}
.row{display:flex;align-items:center;gap:8px}
.rd{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}
.hint{margin-top:16px;font-size:.7rem;color:#333;text-align:center}
</style>
</head>
<body>
<div class="card">
  <div class="logo"><div class="dot"></div>
    <div><h1>BRONNY VIDEO</h1><div class="sub">ESP32-S3 &middot; Railway &middot; YouTube</div></div>
  </div>
  <label for="u">YouTube URL</label>
  <input id="u" type="text" placeholder="https://www.youtube.com/watch?v=..."/>
  <button id="btn" onclick="go()">&#9654; Play on ESP32</button>
  <div id="st"></div>
  <div class="hint">Conversion takes 1&ndash;3 min &middot; ESP32 auto-plays when ready</div>
</div>
<script>
let t=null;
function row(m){return'<div class="row"><div class="rd"></div>'+m+'</div>';}
function show(cls,html){const e=document.getElementById('st');e.className=cls;e.style.display='block';e.innerHTML=html;}
async function go(){
  const u=document.getElementById('u').value.trim();
  if(!u)return;
  document.getElementById('btn').disabled=true;
  show('p',row('Sending to server...'));
  try{
    const r=await fetch('/video/prepare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u})});
    const d=await r.json();
    if(!d.job_id)throw new Error(d.detail||'no job_id');
    poll(d.job_id);
  }catch(e){show('e',row('Error: '+e.message));document.getElementById('btn').disabled=false;}
}
function poll(id){
  clearInterval(t);
  t=setInterval(async()=>{
    try{
      const d=await(await fetch('/video/status/'+id)).json();
      const s=d.status;
      if(s==='queued')       show('w',row('Queued &mdash; waiting'));
      else if(s==='downloading') show('p',row('Downloading from YouTube&hellip;'));
      else if(s==='converting')  show('p',row('Converting: MJPEG + MP3&hellip;'));
      else if(s==='ready'){
        show('r',row('Ready! ESP32 starts within 5 seconds.')+'<br><small style="opacity:.5">Job: '+id+'</small>');
        clearInterval(t);document.getElementById('btn').disabled=false;
      }else if(s==='error'){
        show('e',row('Error: '+(d.error||'check server logs')));
        clearInterval(t);document.getElementById('btn').disabled=false;
      }
    }catch(e){}
  },2500);
}
</script>
</body>
</html>"""


# ── Pydantic models ───────────────────────────────────────────────────────────
class PrepareRequest(BaseModel):
    url: str


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/video", response_class=HTMLResponse)
async def video_page():
    """Open this in a browser to queue a YouTube video for the ESP32."""
    return _HTML


@router.post("/video/prepare")
async def prepare(req: PrepareRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="missing url")
    job_id = uuid.uuid4().hex[:8]
    _update(job_id, "queued")
    threading.Thread(target=_convert, args=(job_id, url), daemon=True).start()
    print(f"[Video] Queued {job_id}: {url}")
    return {"job_id": job_id}


@router.get("/video/status/{job_id}")
async def status(job_id: str):
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/video/current")
async def current():
    """
    ESP32 polls this every 5 s.
    Returns {"job_id":"<id>","ready":true}  when a video is waiting.
    Returns {"job_id":"",    "ready":false} when nothing is queued.
    """
    with _current_lock:
        j = _current_job
    return {"job_id": j, "ready": bool(j)}


@router.post("/video/current/clear")
async def current_clear():
    """ESP32 calls this right after starting playback to avoid replaying."""
    global _current_job
    with _current_lock:
        _current_job = ""
    return {"ok": True}


@router.get("/video/stream/{job_id}.mjpeg")
async def stream_mjpeg(job_id: str):
    path = os.path.join(CACHE_DIR, f"{job_id}.mjpeg")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="video/x-motion-jpeg")


@router.get("/video/stream/{job_id}.mp3")
async def stream_mp3(job_id: str):
    path = os.path.join(CACHE_DIR, f"{job_id}.mp3")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="audio/mpeg")


# ═══════════════════════════════════════════════════════════════════════════════
# HOW TO REGISTER THIS ROUTER IN cloud_brain/main.py
# ───────────────────────────────────────────────────
# Find the line that says:
#
#   app = FastAPI(...)
#
# Then a few lines below it (after the middleware setup), add these TWO lines:
#
#   from video_routes import router as video_router
#   app.include_router(video_router)
#
# That's it. No other changes needed.
# ═══════════════════════════════════════════════════════════════════════════════
