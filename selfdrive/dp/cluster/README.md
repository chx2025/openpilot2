# Dragonpilot Cluster

Standalone raylib cluster UI bundle for openpilot devices.

This bundle is published as `dpegpu00-cluster`, based on
herizon1054/openpilot's `dpegpu00` at
`0cd0b26382a3d07a1028eaa870aad5af7f5e7383`. Only the cluster and its build,
parameter and manager integration are ported; upstream eGPU power limits,
QCOM warp, model compilation, failover, vehicle controls and main UI are unchanged.

Cluster is ported from `dev261007XL-onemiless-cluster` at
`877c57240d277c4c0e279451b905e7cc40f05cc7`, including the latest speed-limit
icon aspect-ratio fix, USB output, native H.264 encoding, settings, radar/lead
indicators. Lead-departure detection and the `READY TO GO` reminder have been
removed; normal radar/lead visualization is unchanged. This removal alone does
not establish the cause of cluster freezes.
Manager autorun remains 20 FPS (offroad rendering remains 1 FPS).
The supervisor retains bounded diagnostic output in `/tmp/cluster.log`
(`CLUSTER_LOG_PATH` overrides it). HUD startup has a 90-second grace period;
after the first progress heartbeat, 30 seconds without render/output capacity
triggers a restart. Shutdown sends TERM, waits five seconds, then escalates to
KILL with another bounded wait. This also recovers processes stuck during USB
replug or cleanup, rather than waiting only for an exit. Hardware/driver stalls
in uninterruptible kernel sleep still require separate diagnosis.
The CPU camera accelerator, model-stall instrumentation and other sunnypilot
runtime changes are not included.

The eGPU status icon reads dragonpilot's `dp_egpu_*` Params and the liveness
of `modelV2` / `modelExt.bigModel`, not sunnypilot's `ChestnutActive` or
`deviceState.chestnutPresent`. Its failure indication is latched until offroad.
Active eGPU labels use `dp_egpu_bundle.short_name` (or `display_name`);
otherwise the label is `S: stock`. Jetlink-specific model labels are not ported.
Optional services absent from this fork, including `longitudinalPlanSP`, are
skipped explicitly; sunnypilot-only speed-limit resolver data is not synthesized.

Run from the openpilot root:

```bash
python selfdrive/dp/cluster_run.py --output usb

python selfdrive/dp/cluster_run.py --output usb --profile-render
```

Useful options:

```bash
python selfdrive/dp/cluster_run.py --output window --width 1920 --height 480
python selfdrive/dp/cluster_run.py --output usb --live-no-can
python selfdrive/dp/cluster_run.py --output usb --usb-codec jpeg --usb-jpeg-quality 68
python selfdrive/dp/cluster_run.py --output usb --input route --route /data/media/0/realdata/0000012e--f190807d64--36 --route-overlay compact --usb-codec h264 --usb-h264-fps 30 --profile-render
python selfdrive/dp/cluster_run.py --output usb --usb-codec h264 --usb-h264-test-pattern-nv12 --duration 20 --fps 10 --usb-h264-debug --usb-h264-slice-max-bytes 4096
python selfdrive/dp/cluster_run.py --output usb --usb-codec h264 --usb-h264-backend ffmpeg --usb-h264-ffmpeg-encoder libx264 --usb-h264-test-pattern --duration 20 --fps 10 --usb-h264-debug
python selfdrive/dp/cluster_run.py --output usb --fps 10 --usb-jpeg-quality 55 --route-overlay off
python selfdrive/dp/cluster_run.py --output usb --profile-render --profile-interval 2
```

`--usb-jpeg-encoder auto` tries optional `turbojpeg` first and falls back to
Pillow. Route replay defaults to `--route-overlay compact`, which shows the
right-side debug panel. Use `--route-overlay off` for performance tests that
should match live rendering cost more closely.

`--usb-codec h264` uses the native Qualcomm V4L2 encoder wrapper in
`system/loggerd/encoder` or the ffmpeg/libx264 comparison path. Native H264
renders directly into the Qualcomm/Venus-aligned NV12 layout before submit, so
the cluster hardware path no longer depends on libyuv or a CPU RGBA-to-NV12
conversion. H264 defaults to the same exact portrait upload geometry used by
the working JPEG/PNG and earlier ffmpeg H264 paths. For a 9.2-inch panel that
means a 462x1920 H264 stream, with no 16-pixel render-size padding unless
`--usb-h264-align 16` is passed explicitly. Native hardware encoding pads only
the encoder input to a 16-pixel boundary by default, so 462x1920 display frames
are fed to V4L2 as 464x1920 and cropped back to 462x1920 in SPS metadata.
The default backend is the native Qualcomm hardware path. It patches hardware
SPS Baseline constraint flags to match the libx264 constrained-Baseline stream
that the TURZX panel accepts, and patches hardware SPS frame-crop metadata for
non-macroblock geometry such as 462x1920. It also asks the V4L2 encoder for
multi-slice output capped by `--usb-h264-slice-max-bytes` so the resulting NAL
sizes are closer to the ffmpeg/libx264 stream accepted by TURZX. The default
H264 bitrate is `auto`, which keeps roughly the same bits per frame as FPS
changes and resolves to `7M` at 30 FPS. The native default is all-I
(`--usb-h264-gop 1`) because TURZX panel corruption measurements improved as
P-frame references were removed. GOP 3 route replay was much better than the
earlier long-GOP runs, and GOP 2 further improved compact-overlay tests, but a
route replay without the overlay showed frequent small block artifacts at GOP 2.
GOP 1 at 6M removed the visible squares on the same route, with only slightly
softer compression detail, and a follow-up GOP 1 / 7M run also stayed clean, so
GOP 1 is the measured stability default for now.
An explicit `8M` route replay was worse and pushed H264 USB
chunk writes into large latency spikes, so the auto cap is limited to `7M`. The
larger `--usb-h264-slice-max-bytes 8192` A/B also looked worse than the default
4096-byte slice cap, and `2048` caused smaller but more frequent smearing, so
keep the default slice setting for normal tests. The
hardware V4L2 rate-control default remains `--usb-h264-rate-control vbr-cfr`;
`cbr-cfr` made frequent small blocks and `--usb-h264-realtime-priority` landed
between VBR-CFR and CBR-CFR, so keep both off for normal tests. The
ffmpeg/libx264 path remains available as a known-good comparison path. Build
the native library before hardware testing:

```bash
scons system/loggerd/libcluster_h264_encoder_bridge.so
```

Use `--usb-h264-backend ffmpeg --usb-h264-ffmpeg-encoder libx264` to compare
the known-good software stream, or `--usb-h264-backend auto` to try native and
fall back to ffmpeg.

The default V4L2 device is
`/dev/v4l/by-path/platform-aa00000.qcom_vidc-video-index1`. Input format
defaults to `nv12`, matching the existing loggerd V4L2 encoder path. Native
NV12 input uses the same Qualcomm/Venus aligned stride, scanline count, and UV
offset calculation as camerad, rather than a compact width-by-height layout.
The previous direct and hidden 32-bit RGB diagnostic input paths have been
removed. The cluster H264 wrapper emits inline SPS/PPS on the first video
packet and on IDR frames, asks for VBR-CFR rate control, constrained
Baseline/CAVLC, and VUI timing when the V4L2 driver accepts those controls, and
the Python sender patches SPS VUI timing and bitstream restriction metadata when
the driver returns a short VUI without timing info. If those baseline controls
are rejected, the native path falls back internally to driver-compatible profile
controls.
`--usb-h264-debug` prints a detailed trace for each early hardware packet:
native callback flags/timestamps/keyframe state, raw and patched NAL summaries,
packetization results, TURZX chunk sizes, and a shutdown summary.
`--usb-h264-diagnose-interval N` prints a compact periodic summary that is less
noisy than debug mode: H264 unit count/keyframes, unit byte rate, chunks per
unit, NAL sizes, native sender queue depth, and USB send latency. Use it on both
native and ffmpeg runs when deciding whether artifacts line up with encoder
output size/cadence or with USB transport stalls.
Keep `--usb-h264-debug` and `--usb-h264-dump` off for FPS/CPU measurements;
they are diagnostic tools and add console/file I/O overhead. The compact
diagnostic log is lighter than debug/dump, but final FPS measurements should
still rerun without it after the suspect interval is identified. With
`--profile-render`, native hardware runs include C++ sub-stage samples such as
`usb_h264.native.convert` and `usb_h264.native.wait_input`.
`--usb-h264-encoder-align 1` disables hardware-only input padding for A/B
testing; the default `16` avoids feeding the Qualcomm encoder a 462-byte NV12
stride while its H264 SPS reports a 464-pixel coded width. In portrait H264
mode, the renderer reads back the aligned encoder size directly so the Python
sender can avoid a per-frame RGBA padding copy while SPS crop metadata keeps
the panel display at the requested 462-pixel width.
`--usb-h264-slice-max-bytes 0` disables the hardware multi-slice request.
Native hardware output is sent as encoder access units, matching the
known-good ffmpeg/libx264 command boundary. The TURZX H264 command `last` flag
is left off to match the working software path.

For a quick H264 transport smoke test, run:

```bash
python selfdrive/dp/cluster_run.py --output usb --usb-codec h264 --usb-h264-test-pattern-nv12 --duration 20 --fps 10 --usb-h264-debug --usb-h264-slice-max-bytes 4096
```

The panel should show red/green/blue/white quadrants on the default NV12
hardware path.
`--usb-h264-orientation landscape` tests direct 1920x462 output, while
`--usb-h264-align 16` deliberately tests macroblock-aligned output such as
1920x464. When `--fps` is omitted, non-live H264 USB runs use
`--usb-h264-fps 5` as the render cap; live H264 runs follow
`ClusterHudLiveFps`. The TURZX display frame-rate command follows the effective
H264 FPS unless `--usb-display-fps 0` is passed explicitly. H264 chunks are no-ACK by
default like JPEG frame uploads; use
`--usb-h264-wait-ack` for strict response diagnostics, or
`--usb-h264-soft-ack` to mimic the vendor video sender's retry/status polling
without failing the run. If the hardware stream is still corrupted, rerun with
`--usb-h264-debug --usb-h264-dump /tmp/cluster_hw_native.h264` and keep the
native packet, packetize, chunk, and final summary lines. Then retry
`--usb-h264-slice-max-bytes 2048` and `1024`; the debug NAL summary should show
several smaller IDR/P NALs instead of one large slice. For 462x1920 streams,
the SPS summary should show `display=462x1920` rather than only the coded
464-pixel macroblock width.
If the hardware SPS summary shows `vui=0`, `timing=0`, or `timing=?`, the
default patch rebuilds SPS VUI timing and bitstream restriction info to match
the selected H264 FPS and the libx264-style no-reorder DPB metadata.

For route replay against a saved device route, run:

```bash
python selfdrive/dp/cluster_run.py --input route --route /data/media/0/realdata/0000012e--f190807d64--36 --route-overlay compact --output usb --usb-codec h264 --duration 60 --fps 30 --profile-render --profile-interval 2
python selfdrive/dp/cluster_run.py --input route --route /data/media/0/realdata/0000012e--f190807d64--36 --route-overlay off --output usb --usb-codec h264 --duration 60 --fps 30
python selfdrive/dp/cluster_run.py --input route --route /data/media/0/realdata/0000012e--f190807d64--36 --route-overlay off --output usb --usb-codec h264 --duration 60 --fps 30 --usb-h264-bitrate 6M
```

To compare native hardware output against ffmpeg/libx264 with the same USB
transport diagnostics, use:

```bash
python selfdrive/dp/cluster_run.py --input route --route /data/media/0/realdata/0000012e--f190807d64--36 --route-overlay compact --output usb --usb-codec h264 --duration 30 --fps 30 --profile-render --profile-interval 2 --usb-h264-diagnose-interval 1
python selfdrive/dp/cluster_run.py --input route --route /data/media/0/realdata/0000012e--f190807d64--36 --route-overlay compact --output usb --usb-codec h264 --usb-h264-backend ffmpeg --usb-h264-ffmpeg-encoder libx264 --duration 30 --fps 30 --profile-render --profile-interval 2 --usb-h264-diagnose-interval 1
```

The ffmpeg/libx264 path is the known-good H264 comparison mode. To make that
explicit while testing, run:

```bash
python selfdrive/dp/cluster_run.py --output usb --usb-codec h264 --usb-h264-backend ffmpeg --usb-h264-ffmpeg-encoder libx264 --usb-h264-test-pattern --duration 20 --fps 10 --usb-h264-debug
```

When the panel still shows a corrupted picture, dump the outgoing stream and
compare it separately:

```bash
python selfdrive/dp/cluster_run.py --output usb --usb-codec h264 --usb-h264-test-pattern-nv12 --duration 20 --fps 10 --usb-h264-debug --usb-h264-dump /tmp/cluster_hw_nv12.h264
ffprobe -show_streams /tmp/cluster_hw_nv12.h264
```

If the dump plays correctly but the panel is corrupted, the remaining issue is
TURZX stream compatibility or USB flow control. If the dump is corrupted too,
the issue is in the V4L2 NV12 submit path or encoder controls.

Keep `--usb-h264-input-format nv12` for native hardware testing. Direct RGB
USERPTR diagnostics were removed after measured device tests showed corrupted
output across direct and hidden 32-bit RGB variants.

Manager autostart passes `--fps 20` by default (`CLUSTER_FPS` remains available
for explicit test overrides). The configured renderer, H264 encoder input,
TURZX display command, and live setting modes default to 20 FPS onroad,
regardless of whether Chestnut is active.
Explicit CLI `--fps`, `--usb-h264-fps`, and `--usb-display-fps` arguments remain
available for diagnostics. Chestnut loading/active no longer imposes an extra
render-rate cap. If the USB pipeline is still busy when a new frame
is due, onroad rendering drops by 5 FPS per overload event (no more than once
every 2 seconds), down to 5 FPS. After 10 seconds without a busy-frame drop, it
recovers by 1 FPS every 5 seconds, up to the configured target. The H264
encoder and display remain at their configured rates; only rendering and frame
submission adapt, so no stream restart is needed. Offroad still hides the 3D
scene and limits HUD rendering and frame submission to 1 FPS, including debug
modes and while Chestnut is active. The top status
row (set speed, follow gap, LFA)
also shows the mici Chestnut icon right of LFA at the same spacing; the row is
shifted left half a slot so it stays centered between the turn signals. Icon (`icons_mici/chestnut*.png`): pulsing white while loading, green
when active, orange when the eGPU model failed, and greyed out when no
Chestnut is connected.
`ClusterHudDebug` controls the autorun output gate: `0` starts external HUD
rendering only while openpilot is onroad, and `1`, `2`, and `3` keep the
always-on debug behavior after power-up. In live input only, `2` also keeps the
top UI icons visible when source data is missing. When output is gated off,
`cluster_autorun` sends TURZX brightness `0` so a stale HUD frame does not
remain visible.
The autorun watcher normalizes locale before this dim-only USB path too, so
vendor USB initialization does not fail before the renderer is launched.
Manager autostart retains the source supervisor's scheduling behavior.
Manual launches through `cluster_run.py` can opt in with `CLUSTER_REALTIME=1`.
With realtime enabled, `cluster_run.py` uses
`ClusterHudCoreMode=0` by default, which maps to cores `1,2,3,4`; mode `1` maps
to all initially allowed CPU cores.
`ClusterHudPriority` controls the common openpilot realtime helper priority with
range `1..99`, default `10`.
Explicit `CLUSTER_REALTIME`, `CLUSTER_REALTIME_CORES`, or
`CLUSTER_REALTIME_PRIORITY` environment values still win.
The manager launches `cluster_autorun` as the single live entry point. The
supervisor passes `--input live` with 20 FPS, H264 automatic encoder selection
and automatic theme. The
`ClusterHudLiveFps`, `ClusterHudEncoder`, `ClusterHudTheme`, and
`ClusterHudBrightness` names are currently read-only integration points unless
another component writes those Params; they are not settings UI by themselves.

The 13 `ClusterHud*` keys are registered as persistent in
`common/params_keys.h` (INT; `ClusterHudRadarInfo`,
`ClusterHudRadarDisplay`, `ClusterHudRadarSourceColor` are STRING so text aliases
work). Unlike sunnypilot, this fork has no `BACKUP` flag. Rebuild the Params
extension with the normal installation build before using the new registered keys.
Until then `Params().get` may reject them and manager
start wipes unregistered files from `/data/params/d`, so the cluster also reads
unset or unknown keys from `/data/cluster_params/<key>` (override with
`CLUSTER_PARAMS_DIR`), one plain text value per file, e.g.:

```bash
mkdir -p /data/cluster_params
echo -n 1 > /data/cluster_params/ClusterHudTheme      # 0 auto, 1 dark, 2 light
echo -n 4 > /data/cluster_params/ClusterHudLiveFps    # all modes currently resolve to 20 FPS
```

Unset keys keep the built-in defaults.
When `--usb-brightness` is omitted, USB launches follow `ClusterHudBrightness`:
`0` auto follows the wide-road camera exposure after samples are available,
using the same ambient-light estimate as the main UI and smoothing changes over
time. The resolved brightness is limited to `3..13` and then scaled to 70%
(so auto tops out at 9 after integer rounding); `1` through `100` are
fixed brightness percentages, also limited to `3..13`.
Missing ambient samples use the default brightness of `13`.
After the boot grace period, live USB output dims to `2` while offroad.
Brightness settings and ambient light are checked once per second onroad.
Offroad, the dim level is applied once after the boot grace period and
brightness polling stops until the next onroad transition. Brightness commands
use no-ACK command `14` during USB initialization and when the resolved
brightness changes.

The launcher defaults to `--input live`, subscribes to openpilot cereal services,
and renders live `carState`, `modelV2`, `radarState`, `radarTracks`,
`controlsState`, `selfdriveState`, `carControl`, `deviceState`, and
`pandaStates`. Front radar
tracks come from `radarTracks` (named `liveTracks` on older cereal trees; the
old `liveDelay`/`liveParameters`/`liveTorqueParameters` names map to
`lateralDelay`/`vehicleParameters`/`lateralTorqueParameters`); the cluster does not directly parse A-CAN CAN-FD
radar track frames for display. Manager/autostart leaves
the live CAN/sendcan subscriptions enabled, but exact LF/RF/LR/RR corner radar
distance now comes only from received Hyundai camera-bus `can` `0x162`/`0x1EA`
messages (`src % 4 == 2`). `sendcan`, ECAN copies generated by
`hyundaicanfd.py`, and returned/rejected `can` echo frames with `src >= 0x80`
are ignored for direct corner parsing so sent presentation frames do not
re-enter as received distance. `--live-no-can` remains a manual diagnostic
option; without raw received CAN, `carState` still provides LF/RF distance and
LR/RR distance when the current cereal schema exposes it. Blindspot booleans do
not create fallback vehicle boxes.
Cluster road speed-limit display treats `carState.speedLimit` from the
vehicle/HDA path as km/h.
Turn-signal arrows are hidden while off and only draw during their blink-on
phase. The top HUD also uses `carState.gearShifter`, `gearStep`, `pcmCruiseGap`,
`selfdriveState.personality`, and `carControl.latActive` to show gear
(`P/R/N/D/1-8`) in a smaller transparent rounded-square outline, front gap bars,
cruise set speed, and the LFA active icon. This top
drive-status row places the ACC, gap, LFA, mode, and Chestnut icons at the
turn-signal height. The gap vehicle uses
`selfdrive/assets/icons_mici/carrot_cruse_gap_trimmed.png` at its source aspect
ratio and is taller than before while the gap bars keep their own size/spacing;
all four gap bars stay visible, sit close together, and bottom-align to the
vehicle while inactive bars are gray and active bars use `#bb3d91`. The ACC
status icon (`assets/speed_limit.png`) has the set speed directly underneath
at the same height as the front-distance label: gray `--` at the same size as
the front-distance label when unavailable, no text in standby until a set
speed arrives, and the set speed at that same size when paused or engaged.
All top-row detail labels, including the LFA angle, share one font size and
vertical center. The enlarged ACC-off LFA keeps its scaled angle below the wheel.
Below the eGPU icon, the active bundle's `short_name` (or `display_name`) is
shown with a `B: ` prefix while the eGPU runtime and model stream are active
(for example `B: TT`). Loading, failed, inactive and disconnected states use
`S: stock`; this fork does not expose a sunnypilot small-model selector.
Names wider than their slot scroll
back and forth with pauses at each end, clipped to the slot; model changes
reset the scroll position.
Model names are sampled off the render thread alongside the system stats.
The eGPU icon also checks live `deviceState`, `modelV2` and `modelExt` health,
not just `dp_egpu_running`: a missing/stale device, a stopped model stream, or
small-model fallback marks a previously running eGPU as failed onroad.
Onroad/offroad follows `deviceState.started`, as in the main UI. Model-message
validity alone does not indicate an eGPU failure (for example during calibration).
Failure stays latched until offroad. Startup waits for device/model messages
before showing active. The icon and B:/S: model selection use the same health
result, refreshed with system stats (normally every 3 seconds).
The focused layout/model-name checks run without a GPU context:
`python selfdrive/dp/cluster/tests/test_status_labels.py`.

Turn-signal timing follows a 0.70-second cycle (0.37 seconds on, 0.33 seconds
off), measured from the reference instrument-panel video. Blink phase uses
elapsed time rather than frame counts, so adaptive FPS and skipped USB frames
do not stretch the cycle. Displayed transitions are still limited by the
actual screen update rate; this does not synchronize phase with the car's lamp.

The live HUD also shows a green `READY TO GO` (departure advisory) banner
centered below the top-row icons and their detail labels for three seconds
when stopped below 0.1 m/s in D/B with ignition/onroad active,
ACC and selfdrive disabled, and the accelerator released. Driver distraction
is not required. The fused `radarState.leadOne` must first remain present
within 8 m without moving away faster than 0.1 m/s for at least 1 second.
After arming, its distance must increase by more than 1 m from the closest
observed distance, with relative speed above 0.1 m/s, continuously for more
than 0.3 seconds of distinct radar samples. This works with the fused lead,
including vision-derived leads; a physical radar is not required.
Car, selfdrive, and radar data must be alive, valid, and no older than 0.5 s,
with no radar errors. Missing leads never trigger the reminder.
The reminder fires once per eligible stop, resets after moving or leaving the
eligible state, and clears on stale data or a lost lead. Data gaps or lead
loss require rearming; no-lead situations do not produce a green-light alert. It is
suppressed by the steering take-control warning. Replay/standby do not trigger
it. This is not traffic-light recognition or permission to proceed: check the
road yourself. No sound, new Params key, or driving-control change is added.

The front-distance label likewise shows `--` while ACC is off, even when
a distance is available; at other times it shows the measured distance or
`na.` when no distance is available.
The icon is orange in standby/paused, green
when engaged and gray when off. Standby is determined by
`carState.cruiseState.available` even before a set speed arrives. This
availability does not enable the 3D scene or move LFA: those still require
the existing set-speed display state. The five top-row icons use evenly
spaced slots between the fixed turn signals; without a set speed, the four
remaining icons spread evenly while LFA is centered.
`TOP_ROW_SCALE` (1.5) enlarges the five status icons, gap bars, and their
labels while the turn signals, MEM/CPU/TEMP metrics, and gear readout keep
their original size. The turn signals stay fixed at x=595/1325. With ACC
displayed, the five status icon centers are 108 design pixels apart and LFA
sits on the x=960 centerline. Without ACC, the four remaining top-row items
spread evenly between ACC and Chestnut while the enlarged LFA stays centered
below them.
The lane-change icon is not drawn; the LFA icon uses `assets/wheel.png`, rotates by
`-carState.steeringAngleDeg`, and is tinted with the same shared top-row
colors as the other status icons. When cruise control has no set speed (`off`), the LFA wheel and angle
move together near the screen center, 50 design pixels below their former
position, and grow to the same on-screen size as before the top-row scale
(four times the unscaled top-row size); the cruise, follow-gap,
and Chestnut top-row items spread out with even spacing. During the layout
transition the angle text stays anchored below the wheel's current bottom
edge, so it never slides over the icon.
The LFA wheel switches to the local orange `assets/wheel_critical.png` while an
active `selfdriveState` alert requests `steerRequired` (take control), then
returns to its normal gray/green icon when that alert clears. Other alerts do
not change the wheel icon.
A normal/experimental-mode icon sits next to Chestnut and follows its
position during transitions. It uses `assets/experimental_white.png` tinted
gray when ACC is off or green in normal mode while ACC is available, and the
original-colored `assets/experimental.png` in experimental mode while ACC is
available. The mode icon displays `std.` or `exp.` underneath at the same
size and height as the front-distance and ACC speed labels. The text reflects
the actual mode even while ACC is off and remains blank until the mode is
known. Live mode uses `selfdriveState.experimentalMode`; offroad uses the
saved `ExperimentalMode` setting. LFA steering angle and front-vehicle distance
labels omit their `deg` and `m` suffixes. Missing gear, angle, distance, and
system metric values are left blank rather than drawing dash placeholders.
The top-row status items share one tint rule. In `B` gear every item — the ACC
icon and set speed, the follow-gap icon with its bars and distance label, the
LFA wheel and angle, and the mode icon with `std.`/`exp.` — is orange whenever
ACC is not off, and gray when ACC is off. Outside `B` gear the follow-gap, LFA,
and mode items are green while engaged, orange while paused, and gray in
standby or off; the ACC icon is green while engaged, orange in standby or
paused, and gray when off. Outside `B` gear while engaged, the colored
`assets/experimental.png` stays untinted so it keeps its original colors, and
its `exp.` label is drawn as orange `e`, green `x`, and red `p.`. The orange `wheel_critical`
take-control icon still takes priority on the LFA wheel. The Chestnut icon
follows its own loading/active/failed colors and is not affected.
The cluster loads its icons, background, vehicle model, and font candidates
from `selfdrive/dp/cluster/assets`; copies of the Chestnut, mode, and
JetBrainsMono assets are kept there so the HUD does not depend on the main
comma UI asset paths.
For 2021-22 Prius/Prius Prime TSS2, the gear display reads the B-gear bit
from incoming bus-0 `GEAR_PACKET` (0x3BC) when `carParams.carFingerprint`
identifies `TOYOTA_PRIUS_TSS2`. The bit is used only while the raw gear is D
and the CAN sample is recent; missing or stale samples fall back to
`carState.gearShifter`. This display-only override does not alter openpilot's
vehicle gear or engagement decisions. Disabling live CAN input also disables
the B display override. Live mode uses a non-conflated, read-only CAN
subscription for the Prius gear packet so slower cluster frame rates do not
skip B/D changes and let the display fall back to D while B is held. A
sub-frame timestamp skew between CAN and `carState` is tolerated while
checking whether the B-gear sample is recent.
When cruise control enters `paused` or `engaged`, the top-row items and the LFA
return to their normal positions and size over 1.5 seconds. The transition
finishes most of its movement in the first second, then settles gradually.
The LFA wheel and angle use a restrained scale settle that never exceeds 105%;
the small distance and mode items do not scale or fade. This does not increase
the actual display frame rate or add additional rendered frames.
Turn-signal positions do not change.
When `--fps` is omitted, `ClusterHudLiveFps` is polled about once per second,
but every supported setting value (`0` through `6`) now resolves to 20 FPS.
Direct route/replay CLI runs also use the 20 FPS default. Explicit `--fps`
remains a fixed diagnostic override. For H264 USB output, changing the
effective FPS exits the current HUD process so autostart can relaunch with a
matching encoder FPS when a launcher is present.
Runs also show a compact lower-right cluster-process CPU overlay by current
core, formatted like `[0(10),1(25)]`, with 2 px bottom/right margins. The
sampler reads the current cluster process and direct child processes only,
avoiding a full `/proc` PID scan in the render loop. Use
`--cluster-core-usage-debug` with `--profile-render` to log the sampler scan
cost plus per-process/core CPU breakdown, or `--no-cluster-core-usage` for an
A/B run without the overlay.
`ClusterHudEncoder` controls the encoder used by manager autostart and by
direct USB CLI runs when `--usb-codec` is omitted: `0` auto tries
native hardware H264 first, then ffmpeg/libx264 software H264, then JPEG when
launched by `cluster_autorun`. Direct CLI auto uses native hardware H264 as the
first encoder choice. `1` forces JPEG, `2` forces native hardware H264, and `3`
forces ffmpeg/libx264 software H264.
Native hardware H264 always uses the direct GPU NV12 render/submit path. If
backend `auto` falls back to ffmpeg, the run uses the software RGBA pipe.
Changing this setting while the HUD is running makes the current HUD process
exit so `cluster_autorun` can relaunch it with the new encoder choice.
The main HUD compact system metrics show memory usage and the highest available
thermal-zone temperature using the three-second system sampler; the SYSTEM
panel also shows those values with CPU core usage.
`ClusterHudScreenMode` controls optional debug views: `0` default, `1` shows
the live debug panel with grouped `LIVE DELAY`, `LIVE TORQUE`, `STEERING`, and
`LATERAL PLAN` rows, `2` shows the system information panel with maximum
thermal-zone temperature, memory, and CPU core usage, `3` shows a large debug
graph selected by `ShowPlotMode` with the
driving scene disabled, and `4`
shows the same graph in the right-side panel while keeping the driving scene.
Mode `3` also hides the speed, accel, clock, turn-signal, and git HUD so the
large graph uses the available center/right height with only a small margin.
Mode `4` keeps the driving HUD and uses the maximum right-side panel height with
the same margin. Modes `1`, `2`, `3`, and `4` suppress the route overlay so the
selected debug view remains visible.
`ClusterHudRadarInfo` controls world radar/vehicle speed and distance labels:
`0` off, `1` speed for vehicle boxes only, `2` speed and distance for vehicle
boxes only, `3` speed for all vehicle boxes and raw radar points, and `4` speed
and distance for all. `ClusterHudRadarDisplay` controls raw radar point
presentation: `0` averages nearby points with nearly matching speed/position,
and hides raw radar vehicle boxes that overlap already-rendered detected
vehicles such as front-center `radarState` leads; `1` leaves raw points
unmerged for detail checks, including radar vehicle candidate boxes and
radar-to-detected-vehicle speed merges. Vehicle/radar metric labels sit closer
to the point/box top so speed and distance are less high above the vehicle.
`LR`/`RR` rear-corner detections render as normal vehicle boxes at their actual
detected positions. The older fixed rear-tire-depth 2D arrow/label is removed.
The default drive camera sits closer to the ego roof, lower than the earlier
high view, tilted downward, and shifted `5m` forward so route/live scene space
is easier to see. Detected vehicles, radar points, and desired-distance markers
compress signed longitudinal placement by `0.5` in the rendered scene only, so
actual `20m` and `-10m` draw at rendered `10m` and `-5m`. Distance labels keep
the actual signed longitudinal values. The ego vehicle is drawn half a vehicle
length behind the raw `0m` reference so its front bumper aligns to that
reference. The temporary radar-zero, lane-start, and ego-zero debug marker bars
are no longer rendered.
`ClusterHudCameraViewMode=0` keeps this current camera. Mode `1` uses a
pulled-back ego-bottom camera view for cars without rear radar.
The console refresh line prints `cam=<mode>` so live param changes can be
confirmed while the HUD is running.
The top HUD row uses 128x128 turn-signal assets with a 30px top margin. The
follow-gap icon shows the nearest detected vehicle distance in the ego lane
below it, and the LFA wheel shows the signed steering-wheel angle below it.
With the acceleration gauge hidden, the speed digits and `km/h` label share
an x=255 center in the left panel.
When both raw camera-bus ADRV `0x1EA` and CCNC `0x162` corner messages are
fresh, ADRV is preferred for LF/RF/LR/RR distance in the Hyundai `carState`
DBC parsing path. The cluster consumes the DBC-parsed `carState` corner fields
first; route replay/raw-CAN fallback also decodes `0x162`/`0x1EA` through the
Hyundai CAN-FD DBC instead of hand-coded bit positions.
Road/lane/radar geometry still starts far enough behind that rendered bound so
the rear lane-start seam sits below the visible bottom edge.
Front-center `radarState` lead overlap uses a wider vehicle-sized tolerance
than corner radar overlap, and default mode also collapses overlapping
front-center detected vehicle boxes so source-split front radar/model
reflections merge cleanly.
`ClusterHudRadarSourceColor` controls vehicle box colors:
`0` keeps all vehicle boxes gray, while `1` uses source colors: radar track
vehicles yellow, `radarState` front/SCC radar leads red, camera-sourced vehicle
leads light blue, comma model leads dark blue, and ADAS corner detections from
`0x162`/`0x1EA` green.
The locked front vehicle (`radarState.leadOne`, `DetectedVehicle.primary`)
only renders green while ACC is actually engaged
(`state.cruise_display_state == "engaged"`); `leadOne` can be populated by
openpilot even while cruise is off/paused, so `vehicle_box()` gates the green
highlight on an `acc_active` flag (derived from `cruise_display_state` at the
`build_cluster_scene()` call site) instead of `primary` alone, and falls back
to the normal source color otherwise.
Radar samples whose distance and left/right offset are both zero are treated as
empty/default data and are not drawn as radar points or vehicle boxes.
Live `radarPoint` candidates must remain present for 0.10 seconds before they
are displayed; `radarState` `TARGET2` candidates retain their 0.25-second
stability delay. Vehicle log version 3 records `filter_pending`,
`filter_passed`, `filtered`, and `filter_expired` stability events in the same
JSONL file. Stability is measured across distinct sensor updates rather than
render frames. Each record includes a `record_type`, threshold, sample count,
candidate duration, raw position, speed, probability, and sensor source for
later tuning.
Unverified `modelV2.leadsV3` candidates (no nearby stable radar point support)
now also use a short 0.18-second stability delay before rendering, while
radar-supported model leads continue rendering immediately.
Because raw `radarPoint` data has no upstream fusion/hold logic like
`detected_vehicles`, a point that just cleared the stability delay can still
disappear on the very next `liveTracks` sample. To avoid that residual
flicker, radar points that just passed the stability filter are held at full
probability for 0.15 seconds after they stop appearing in the sensor data,
then fade their probability down to a 0.40 floor over another 0.15 seconds
before being dropped, mirroring the hold/fade behavior already used for
`detected_vehicles` leads.
Vehicle log version 4 adds diagnostic-only `stability_gate` and `render_phase`
fields to `rendered_vehicle` records (the `stability_filter` records already
encode the same distinction implicitly via `source_base` and the
`filter_pending`/`filter_passed`/`filtered`/`filter_expired` event names).
`stability_gate` is `"immediate"` for objects that never needed a stability
delay (`TARGET`, other `radarState`/`carState`/corner-radar sources, and
radar-supported model leads) or `"delayed"` for objects that had to clear a
stability filter (`TARGET2`, `radarPoint`, and unsupported model leads).
`render_phase` is `"active"` while the sensor is currently reporting the
object, `"held"` while it is missing but still shown at full confidence
during the hold window, or `"fading"` while its probability is ramping down
toward the minimum before being dropped. `rendered_vehicle` `disappeared`
events also add a `disappear_reason` derived from the last known
`render_phase`: `"faded_out"` means the object disappeared as designed once
its hold/fade window ran out, while `"scene_drop_while_active"` or
`"scene_drop_while_held"` mean the scene-composition step (lane filtering,
merging, hidden-by-another-box, etc.) stopped drawing the object even though
`OpenpilotLiveSource` was still actively reporting or holding it -- a useful
signal for telling stability/hold-fade flicker apart from rendering-layer
flicker.
Live driving logs using the fields above showed most flicker was actually
`scene_drop_while_active`, and the dominant cause was the fixed 3-lane
display range check (`FRONT_VEHICLE_LANE_RANGE_LANES = 1.5`,
`vehicle_in_forward_display_lanes()`/`point_in_forward_display_lanes()` in
`cluster_scene.py`): a hard `abs(lane_offset) < 1.5` cutoff recomputed every
frame with no hysteresis, so an object whose lane-offset estimate hovers at
the boundary (from road-curvature/model noise) popped in and out of the
display every frame even though the sensor never stopped reporting it.
`OpenpilotLiveSource._smooth_scene_state()` now resolves this with a
hysteresis band, stamping a `DetectedVehicle`/`RadarPoint.in_display_lanes`
flag each frame: an object must cross inward past 1.5 lanes to become
visible, but is only removed once it crosses outward past
`FRONT_VEHICLE_LANE_RANGE_LANES + LANE_RANGE_EXIT_HYSTERESIS_LANES` (1.65
lanes). `vehicle_in_forward_display_lanes()`/`point_in_forward_display_lanes()`
use this flag instead of recomputing the raw threshold whenever it is set;
it is `None` (falls back to the original raw-threshold behavior, no
hysteresis) for paths that bypass `OpenpilotLiveSource`, e.g. route
replay/simulator. `merged_radar_point()` (used to fuse nearby raw radar
points into one box outside detail mode) re-derives this flag from its
merged/averaged position rather than blindly trusting a constituent's flag,
since a merge group can span both sides of the boundary.
A follow-up driving log showed the lane-range hysteresis above barely moved
the overall `scene_drop_while_active` rate (62.5% vs. 63.2% before), and
the disappearing radar points' logged positions/confidence did not actually
cluster at the 1.5-lane display boundary or the 0.80 confidence-display
threshold as first suspected, ruling those out as the dominant cause after
all. Vehicle log version 5 adds more `radarPoint` classification diagnostics
to `rendered_vehicle` (and `stability_filter`) records to narrow this down
further: `valid`, `valid_count`, `in_my_lane`, `motion_consistent`, and
`promotion_held` mirror the same-named `RadarPoint` fields, and
`vehicle_candidate` mirrors `radar_point_is_vehicle_candidate()`'s live
result for that exact point/box -- the multi-branch "is this radar point
actually a vehicle" classifier (distance/lateral bounds, valid-count,
motion-consistency, in-lane, probability, road-edge distance,
stationary/moving classification) that gates whether a `radarPoint` ever
becomes a candidate box at all. If a future log shows `vehicle_candidate`
flipping to `false` for points logged `render_phase="active"` right before
a `scene_drop_while_active` disappearance, that pinpoints this classifier
(rather than the lane-range boundary) as the flicker source.
`merged_radar_point()`'s synthetic point recomputes `vehicle_candidate`
directly (rather than merging constituents' values) since that synthetic
point -- not any individual raw constituent -- is what
`radar_point_is_vehicle_candidate()` actually evaluates outside detail mode.
The v5 live log showed that most active radarPoint drops still had
`vehicle_candidate=true`, so the scene-composition path was investigated
separately. A low-confidence detected vehicle could be excluded from
`detected_vehicle_boxes` (below `FRONT_VEHICLE_MIN_CONFIDENCE`) but still hide
a nearby radarPoint before rendering. Scene composition now uses only
confidence-qualified detected vehicles for radar hiding and merged-label
suppression; an undrawn low-confidence model object can no longer make a valid
radarPoint disappear without a replacement vehicle box.
These `rendered_vehicle`/`stability_filter` JSONL diagnostics are a tuning aid,
not a runtime feature, and they are now **off by default**. When enabled they
build a rounded payload dict for every tracked object on every sensor update
(the `radarPoint` payload even re-runs the multi-branch
`radar_point_is_vehicle_candidate()` classifier) and append line-buffered JSON
to `/data/media/0/cluster_vehicle_objects.jsonl`, which costs CPU and flash
writes on every drive. Set `CLUSTER_VEHICLE_LOG=1` (accepted truthy values:
`1`, `true`, `yes`, `on`) to turn the logging back on for a tuning session;
`CLUSTER_VEHICLE_LOG_PATH` still overrides the output file.
The 3D driving scene is hidden while ACC is off, but the background image remains
visible. On ACC activation, 3D rendering remains hidden until the LFA/top-row
layout transition has completed, preventing the centered LFA animation from
overlapping the 3D scene. While ACC is engaged, the ego-lane floor uses the
original solid green fill (no moving rainbow animation). During ACC lane-change
states the planned path, ego-lane floor, target-lane floor
highlight, ego vehicle box, and detected/radar vehicle boxes follow the
animated lane-change offset.
Radar-track vehicle classification rejects points outside model road edges, but
does not require in-road points to sit near the road-edge line; center-lane
points can classify as vehicles when probability/in-lane data or moving radar
radar evidence is sufficient. Points near or slightly outside a road edge can
still classify as vehicles when their counter is stable, absolute speed is
vehicle-like, and acceleration stays within about +/-5 m/s^2, even if the radar
radar probability is low.
Lane and road-edge rendering keeps model geometry visible instead of filtering
by `laneLineProbs` or `roadEdgeStds`, avoiding distracting HUD flicker when
model confidence jitters. Lane markings are still suppressed when their
lateral offset falls outside a valid model road-edge boundary. Dashed lane
markings are phased so the visible rear bound starts with a line segment
instead of a gap.
When `carState.leftLaneLine` or `carState.rightLaneLine` carries camera/CAN
lane color codes, the current lane markings use that color first
(`+10=white`, `+20=yellow`) before falling back to the cluster model colors.
The planned path draws `longitudinalPlan.desiredDistance` as a magenta
horizontal bar across the current lane width at the matching forward position.
Changing `ClusterHud` to another supported mode or `0` makes the running HUD
exit; cleanup sends TURZX brightness zero before releasing the USB device.
When autorun passes a HUD mode, USB open is pinned to that mode's TURZX PID
(`1 -> 0x0092`, `2 -> 0x0123`) so a second connected TURZX panel is not opened
by the vendor library's generic device scan.
If frame or H264 chunk writes report that the USB device was disconnected, the
active HUD exits instead of trying to recover in-process. Autorun calls the
launcher in non-exiting mode so the error returns to the watcher loop, letting
`cluster_autorun` wait for the same PID and relaunch after replug.

The bundled TURZX code includes only the Python vendor library. The openpilot
device uses the system `libusb-1.0.so` through `pyusb`. Pure-python `pyusb`
1.3.1 and `pyserial` 3.5 (BSD) are vendored in `.vendor/pydeps` and appended to
`sys.path`, so they are only used when the device venv lacks them.

`cluster_autorun` always launches the HUD with `RAYLIB_BACKEND=comma`
(Adreno GPU through DRM/GBM), like CarrotPilot. In USB output mode the HUD only
renders into render textures and never calls `end_drawing()`, and the comma
backend defers its modeset to the first buffer swap, so the openpilot UI keeps
the panel. There is no CPU (headless/llvmpipe) fallback because it only reached
~1-2 FPS on device; if the comma backend fails, the HUD exits and autorun retries
every 5 seconds.

On the comma backend the native H.264 path renders NV12 straight into the
Venus encoder's input DMA-BUFs (`cluster_gles_dmabuf.py`, ported from
CarrotPilot): each encoder buffer is imported once as an `EGLImage`-backed
framebuffer, the Y/UV pack shaders draw into it, and a GL fence is waited on
before the buffer is queued, so there is no `glReadPixels` or CPU copy. If the
import fails it falls back to GPU readback. Set `CLUSTER_NV12_DMABUF_OUTPUT=0`
to disable it.

The renderer loads the fonts bundled in `selfdrive/dp/cluster/assets/fonts`
(OrbitronBlack, then GeistMono-Light, then KaiGenGothicKR-Bold), falling back to
the same names under `selfdrive/assets/fonts`, then
`JetBrainsMono-Medium.ttf` and system/platform fonts.

USB frame upload runs in no-ACK mode by default because some TURZX panels accept
image data but never return a frame-upload response. Use `--usb-wait-frame-ack`
only when testing a panel/driver combination known to reply after each frame.

Manager autorun uses H264 at 20 FPS, trying the native encoder first and
falling back to ffmpeg when the native bridge or V4L2 encoder is unavailable.
Automatic bitrate at 20 FPS is 4.68 Mbps (about 0.585 MB/s before transport
overhead); higher diagnostic rates retain the 7 Mbps cap. This is a target refresh rate,
not a guaranteed measured rate; busy-frame dropping still applies with Chestnut.
The normal comma installation build and prebuilt release build both compile
`system/loggerd/libcluster_h264_encoder_bridge.so` automatically.
To rebuild only that target during development, run
`scons system/loggerd/libcluster_h264_encoder_bridge.so`. When
Chestnut is loading or active, H264 USB chunks are capped at 32 KiB with a
2 ms yield between chunks so its transfers can use the shared bus. The HUD
uses a process-shared lock at `/tmp/carrot_usbgpu_bus.lock` for cooperating
display senders. Upstream eGPU/tinygrad transfers are not modified to acquire
this lock; the chunk pacing is not a guarantee of serialization with inference.

When the asynchronous JPEG sender or native H264 sender is busy, the HUD
skips the new USB frame before readback/encoding, without waiting for capacity.
Native H264 admits the next frame only after the previous encoded frame's
packets have finished sending (including a packet currently in a USB call).
Packets are queued whole, so a large frame is not rejected merely because it
needs more than eight USB chunks. No H264 bytes or reference frames are dropped
after encoding; the existing chunk yielding and USB lock remain in effect.
The ffmpeg fallback writes raw frames on a worker with no pending-frame FIFO;
new frames are skipped while that worker is writing. ffmpeg/OS pipe buffering
still exists, so that fallback does not guarantee native's one-frame-in-flight
bound. Synchronous JPEG/PNG paths naturally block rather than queue frames.
Missing native encoder output for three seconds raises an explicit error for
autorun to restart the HUD; normal backpressure is not treated as an error.
The model label shows `S: stock` only after valid, live `modelV2` and
`modelExt.bigModel=false` publications are observed onroad. Loading, failed,
waiting, and offroad states do not imply that stock inference has started.
The Chestnut failure icon stays latched for the drive even after stock output
recovers. Built-in USB big-model output also sets `modelExt.bigModel=true`,
so it cannot be mistaken for a small model.
The regular status log's cumulative `usb_dropped` counter reports skipped
busy-frame attempts. This reduces stale HUD backlog, not GPU/USB fault timeouts,
and cannot cancel an already-running USB transfer.
