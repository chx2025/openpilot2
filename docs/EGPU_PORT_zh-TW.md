# eGPU 大模型移植說明 v2：sunnypilot → openpilot-dp0111pre（含 Jetlink v11）

> 來源：`sunnypilot-master`（2026-10-14 快照）的 chestnut / eGPU 功能（模型管理、大模型選擇器、設定頁、側邊欄與行車圖示）
> 依據：`AmyJeanes/sunnypilot@173d90b`（port commaai/openpilot#38862：warp 在 QCOM、只送裁切影像過 USB）
> 參考：jetlink（dp 內 v0.8.5 的 `registry/lfs.py`、jetlink-main v0.8.6 的 `spec.py` / `queues.py` / `tests/tiny_model.py` / `catalog/extra_big_models.json`）
> 目標：`openpilot-dp0111pre`（2026-10-14 新分支）**＋ Jetlink v11 覆蓋**
> 硬體：comma 3X（tici 大 UI）＋ USB eGPU（chestnut，USB ID `ADD1:0001` / `3801:0001`）
> 日期：2026-10-15

---

## 0. v2 和 v1 的差別（一句話）

v1 下載 sunnypilot / Amy **預編譯的 pkl**，只有 Cinque v1/v2 在 3X 上能順跑；
v2 改成**下載模型的 onnx、在車上自己編譯一次**，warp 永遠編給 QCOM，**任何模型（含 Cinque V3 以後的新模型）都只送裁切影像過 USB**。機器上的 tinygrad 從三份減為**兩份**。

| | v1 | v2 |
| --- | --- | --- |
| 模型來源 | Amy 的 chestnut v25 pkl | **與 Jetlink 共用同一份清單**（`JetlinkCatalog`：sunnypilot 各版清單＋jetlink 額外清單），只取名稱與 commit；實體是 onnx |
| 3X 上「只送裁切影像」 | 僅 CTM / CTMV2 | 全部 |
| Cinque V3、Mountain Dew V2、ResAction | 不支援 | 支援（stateful 圖） |
| 隨附 tinygrad | sunnypilot@f6fc4e3f（`tinygrad_sp/`） | sunnypilot@9cd40014（`tinygrad_spm/`，＝ sunnypilot-master 的 tinygrad_repo，已逐檔比對一致） |
| 刪除單一模型 | 無 | 選單內可刪（編譯檔＋onnx） |

---

## 1. 機器上的 tinygrad（兩份，互相隔離）

### dpegpu00-cluster 開機診斷

- `hardwared` 首次發布 offroad `deviceState` 後，至少保持 offroad 3 秒，才允許依當下點火及全部既有啟動條件進入 onroad。以 monotonic 時間計算，不阻塞硬體／Panda 資料接收，也不偽造點火；診斷條件名為 `initial_offroad_complete`。計時只在此程序啟動時進行一次，正常熄火再點火不重算，重啟 `hardwared` 則重算。這不會先載入小模型，也不保證 eGPU 在 3 秒內就緒或能解決模型載入停滯。

- 忽略啟動時產生的 `jetlink` 連結、字型快取、manager log，以及無作用的舊 `prebuild` 檔案，避免這些檔案讓乾淨版本每次都重跑 SCons。`prebuild` 不會啟用免編譯；真正原始碼修改與其他未追蹤檔案仍觸發建置。成功建置且工作目錄乾淨後，才寫入原有 commit 快取。
- SCons 主機後端探測只初始化 CUDA／QCOM／CPU，不再透過全裝置列舉順便初始化 AMD；30 秒未返回即明確失敗，不假裝探測成功。必要的 USB 大模型建置仍保留，不能保證所有 GPU 編譯都不會停滯。
- 建置失敗不再繼續啟動 manager，避免執行未完成的建置結果；保留原有 build 錯誤畫面。
- `hardwared` 未 started 時，在啟動條件改變或每 30 秒記錄 `Startup waiting`，含不成立條件、Panda 存活／有效性、點火與 harness 狀態。此記錄不改動點火、安全或溫度閘門。

| tinygrad | 位置 | 誰用 |
| --- | --- | --- |
| dp 原本的 | `tinygrad_repo/` | 小模型、DM、jetlink 的 comma 端 warp、scons 建置 |
| sunnypilot@9cd40014 | `dragonpilot/egpu/tinygrad_spm/`（`tinygrad/` 套件＋`examples/openpilot/{helpers,compile_onnx,compile_warp}.py`，約 18 MB） | **只有** `compile.py`（編譯）與 `modeld_egpu.py`（執行），兩者都是全新直譯器 |

為什麼要全新直譯器：`system/manager/process.py` 的 `prepare()` 會在 manager 內 preimport 所有 PythonProcess，fork 出來的子行程繼承 dp 的 `tinygrad`。所以 `modeld_egpu` 是 **NativeProcess**，`compile.py` 由 `egpu_manager` 以 subprocess 啟動；兩者第一行 `import tg_env` 把隨附版放到 `sys.path[0]`，並 assert 真的載到隨附版。

### dpegpu00-cluster：開機編譯的 USB ID 相容性修正

兩份 tinygrad 的 USB AMD 後端均接受 `ADD1:0001`、`3801:0001`、`ADD1:0002`，與 `selfdrive/modeld/helpers.py` 的偵測清單一致。
原版 dp tinygrad 只接受 `ADD1:0001`，因此接著 `3801:0001` Chestnut 開機時，SCons 會偵測到 eGPU、啟動內建大模型編譯，卻在底層報 `AMD:0 does not exist (0 devices available)`。
此修正保留原 ID 的枚舉優先順序、裝置可見性篩選及無裝置時的錯誤；不改模型、功耗或編譯流程，也不保證解決其他 GPU 初始化／推論停滯。

不開啟硬體的回歸測試：`pytest selfdrive/modeld/tests/test_usbgpu_device_ids.py`。完整 AMD 編譯與接著 Chestnut 開機仍需在裝置上驗證。

### dpegpu00-cluster：使用中 USB 失聯的 fallback

`egpu_manager` 在 onroad 偵測到 USB 裝置消失，且大模型正在載入或執行時，會同步設定 `dp_egpu_failed`，交由既有的單向路由停止 eGPU 程序並啟動 stock modeld。本趟不因 USB 重插而自動切回大模型。
此檢查不依賴可能已卡住的 GPU 程序自行回報錯誤；但仍受 1 Hz USB 輪詢限制，不能偵測所有短暫斷線或 USB 仍存在的 GPU 停滯，也不代表已修正失聯的硬體根因。
fallback 啟動的 stock modeld 會讀取 `dp_egpu_failed`，強制停用內建 USB 大模型與 Jetlink attach，避免 Chestnut 已重插時再次載入大模型。未失敗時保留原有內建 USB 模型選擇。
回歸測試：`pytest selfdrive/modeld/tests/test_egpu_disconnect.py`。桌上測試不等同道路安全驗證。

### dpegpu00-cluster：首次相機推論與停輸出保護

2026-10-17 的 CTV3M 試車曾在載入與 dummy 暖機完成後、第一筆實際模型輸出之前報 `NPY hang detected`；後一次啟動則連續輸出約 32 分鐘。此錯誤是同步等待逾時的表徵，現有日誌不足以判定是 AMD、QCOM 或主機緩衝區的底層根因，也未記錄到該次 USB 消失。

- 暖機重設 hidden state 後，明確同步 AMD、warp 裝置及 NPY，完成已排入的工作後才釋放 dummy 相機映射、交給主執行緒。
- 第一張真實相機畫面的映射、warp、推論及發布完成後，才升為 FIFO 並設定 `dp_egpu_running`；等待相機、載入與首次推論期間保持 `dp_egpu_loading`，避免「已載入」被誤標為「正在正常輸出」。
- `egpu_manager` 獨立監看 `modelV2` 與大模型 `modelExt` 的時間戳：啟動後 150 秒仍無首筆發布，或 running 後任一輸出停滯至少 5 秒，即明確記錄錯誤、同步設定本趟失敗，走既有單向小模型 fallback。這不依賴 USB 消失或卡住的 modeld 自救；監測仍受 1 Hz 輪詢影響，不代表允許停輸出期間繼續控制。
- 首張真實相機推論記錄 camera mapping、QCOM warp／readback、AMD upload／inference／readback 與解析階段；例外保留階段及完整 traceback，不靜默重試或自動切回大模型。

回歸測試：`pytest selfdrive/modeld/tests/test_egpu_startup.py selfdrive/modeld/tests/test_egpu_disconnect.py`。這些修正補上初始化交接與失敗處理缺口，不等同已根治底層 `NPY hang`；接著 Chestnut 的冷開機、首次真實影像及斷線 fallback 仍需在裝置驗證。

編譯結果的快取鍵含 tinygrad ref（`tg9cd40014v1`）：之後若更新隨附 tinygrad，舊編譯檔自動作廢、使用中的模型自動重編。

---

## 2. 兩種圖結構都支援

| 結構 | 模型 | 編譯方式 |
| --- | --- | --- |
| **stateful**（openpilot #38916，2026-09-15 起） | Cinque Terre V3、Mountain Dew V2 Preview、ResAction Preview，以及之後的新模型 | 原樣用 sunnypilot-master 的 `examples/openpilot/compile_onnx.py`（`--device-input *`、out-of-band），所有輸入放在 eGPU；`state_*` 由 `next_state_*` 原地更新（照 sunnypilot-master `ModelState` 的 aliasing） |
| **queued**（到 Cinque Terre V2 為止） | Lebowski、Tee Time、BMRLNAP 系列、Sad、CTM、CTMV2 | `compile.py` 包一層 JIT：歷史佇列（img / big_img / features / desire）留在 eGPU 上，邏輯照 Amy／上游 `compile_modeld.py` 的 `make_input_queues` / `shift_and_sample`；hidden state 由 runner 回授（同 Amy 的 `prev_feat`） |

**warp**：sunnypilot-master 的 `examples/openpilot/compile_warp.py`，`--layout yuv420 --frames 1`，transform 在 host（NPY），用 `DEV=QCOM` 編。每個相機解析度一份（tici/3X 1928×1208、mici 1344×760），**與模型無關，整台車只編一次**。執行時相機 buffer 以 `Tensor.from_blob` 就地交給 warp（照 Amy commit），不複製整張影格。

**每幀資料流**：相機 NV12（留在 comma）→ QCOM warp → 兩張 6×128×256 裁切（約 393 KB）＋純量（desire、traffic convention、action_t，queued 另加 hidden state 64 KB）→ **一次** host→eGPU 上傳 → eGPU 模型 JIT → 輸出回 host 解析。

---

## 2b. eGPU 功耗：採用 #38992 的 100 W 上限，**刻意不採用** AmyJeanes/openpilot-upstream@e9fa764

隨附 tinygrad（`tinygrad_spm` @9cd40014）`amdev.py` 開機時的行為（已對照原始碼）：

| 設定 | tinygrad 行為 | 大模型實測（Amy，3X） | chestnut 輸入電流 |
| --- | --- | --- | --- |
| 不設 `AM_POWER_LIMIT`（v1～v3） | `set_clocks(-1)` 鎖最高時脈，**沒有功耗上限**（SMU 預設上限） | 最快 | 最高 |
| `AM_POWER_LIMIT=100`（openpilot #38992，**本版採用**） | `set_power_limit(100)`＋`set_clocks(None)`，時脈交給 SMU | 約 1.2 GHz、31 W，p50 約 41.4 ms（3X 上 42.6 ms） | 約 5 A |
| ＋e9fa764 鎖最高時脈 | 100 W 上限內鎖最高時脈 | 約 1.5 GHz、49 W，p50 約 36.5 ms | 突波約 12 A（約 127 W） |

- **為什麼不用 e9fa764**：純油車的 12 V 電氣系統撐不住約 12 A 的突波。`modeld_egpu.py` 只設 `AM_POWER_LIMIT=100`（`setdefault`，可用環境變數改），不呼叫 `set_clocks(-1)`。
- **代價**：Amy 在 3X 上量到模型約 42.6 ms，加上前段處理接近 50 ms 幀預算，曾出現 1% 掉幀與 modeldLagging。上車請觀察 modeldLagging／掉幀；若仍吃緊，可改更低的 `AM_POWER_LIMIT`（電流更低、模型更慢），或改用較小的 766 MB 級模型。
- 注意：v1～v3 根本沒設 `AM_POWER_LIMIT`，那其實是**耗電最高**的狀態（最高時脈、無上限）。本版起改為 100 W 上限、SMU 自調。

## 3. 模型清單與 onnx 從哪來

- **清單（與 Jetlink 共用）**：直接用 jetlink 的 `JetlinkCatalog` 參數，由 `dragonpilot/jetlink_adapter.refresh_catalog()` 產生，內容和 jetlink 手機／Mac App 列的一樣：sunnypilot 釘選的 chestnut 清單與之後每一版，加 zoompilot/jetlink 的 `catalog/extra_big_models.json`；它同時把每個模型的 onnx pointer 解析進 `JetlinkModelPointers`。eGPU 頁和 Jetlink 頁的「重新整理模型清單」呼叫的是同一個函式，任一邊更新兩邊都看得到；`egpu_manager` 在 offroad 時每小時（及開機後第一次）自動更新一次。還沒更新過時，清單只有 jetlink 的預設模型 Cinque Terre V3。用實際檔案驗證（jetlink 的 merge 規則套在 v25＋v27＋extra）：16 個模型，新到舊為 MDV2、RESACT、CTV3M、CTMV2、CTM … LM。
- **onnx**：用 dp 內 jetlink 的 `jetlink/registry/lfs.py`：`ref`（comma openpilot commit）→ `big_driving_supercombo.onnx` 的 git-lfs pointer（stateful 的新 commit 改從 commit 標題、diff 或 PR 找出 export，再到 HF `commaai/openpilot_driving_models`）→ 依序向 GitLab、HF 的 LFS 伺服器取連結 → 下載（`.part` 續傳、sha256 驗證）。
  - 實測（本沙盒）：v27 的 13 個模型都解析到 pointer（766 MB／1757 MB 兩級），CTM `e8d82173…`、CTMV2 `09d080f3…` 與 Amy README 寫的 onnx 一致。CTV3M 需要 github.com 的 commit patch，沙盒被擋所以沒驗；jetlink 在車上就是這樣用的。
  - onnx 存在 **jetlink 的資料夾** `/data/media/0/models/jetlink/<sha256前16碼>.onnx`，jetlink 已下載過的直接重用，兩邊共用一份。pointer 也會先查 jetlink 的 `JetlinkModelPointers` 快取。

- **預設模型 CTV3（v5 起，方案 B）**：CTV3（comma `bf3e3631`）的 commit 只附預編譯 pkl，沒有 onnx；它的 onnx 在 HF `commaai/openpilot_driving_models` 的 `f78ed37d-afad-4dbc-8050-40ea885eedde/12864/big_driving_supercombo.onnx`，oid `404a18cfd86d29637d20c697dfde245bb47c666ae016730ab674c65f4d1e1aa4`、766,354,845 bytes（2026-10-15 以 HF API 查證，與 jetlink `fetch_export_pointer` 的找法相同）。這組 pointer 寫在 `constants.KNOWN_POINTERS`，不必上網解析。
  - **第一次開機自動下載＋編譯**：熄火、eGPU 開關開、**偵測到 eGPU**、還沒有使用中的模型時，`egpu_manager` 自動排入 CTV3，等於使用者自己點選它。完成後清除 `dp_egpu_auto_default`，之後不再自動做。
  - **不會自動下載的情況**：沒接 eGPU 的 comma（開關預設是開的，不能讓每台都多下 766 MB）；使用者在 eGPU 頁選過任何模型、選「不使用大模型」、按「取消」或刪除模型（都會清除 `dp_egpu_auto_default`）。
  - 下載失敗（網路）10 分鐘後重試；上路中斷、熄火後續傳。
  - **不用 git LFS 的原因**：dp 的內建大模型路徑（`SConscript`）要的是分開的 `big_driving_vision/policy.onnx`，CTV3 是單一 supercombo，只能交給 egpu 的 `compile.py`；而且用 zip 覆蓋安裝時，LFS 檔只是文字指標，不會下載。

---

## 4. 使用流程

1. **offroad**：設定 → eGPU → 大模型「選擇」→ **一層清單**（新到舊，與 Jetlink 頁的大模型選單相同，沒有資料夾），每個模型標示「（使用中）」「（已編譯）」「（已下載）」→ 點選即「使用」：已編譯就直接切換，否則自動下載並編譯。最上方「不使用大模型」可改回內建小模型。
   - **刪除模型**：設定頁「刪除模型」→ 只列出已下載或已編譯的模型 → 選一個 → 確認後刪除它的編譯檔與 onnx；刪除使用中的模型 → 改由內建小模型駕駛
     - onnx 放在與 jetlink 共用的資料夾，所以刪除後 jetlink 若要用同一個模型也得重新下載（eGPU 插著時 jetlink 本來就不會啟動）。warp 與模型無關，不會被刪。
2. `egpu_manager` 依序執行，**每一步若結果已在磁碟上就跳過**：查 pointer → 下載 onnx → 編譯 warp（第一次才會）→ 編譯模型 → 設為使用中。「處理狀態」會顯示：查詢模型中／下載 onnx N%／編譯 warp m:ss／編譯中 m:ss／等待 eGPU 接上以編譯／已完成／失敗／已取消，處理中可以按「取消」。
3. **eGPU 沒插**：可以先下載 onnx，狀態停在「等待 eGPU 接上以編譯」；插上後（offroad）自動接著編。
4. **上路**：第一個 onroad tick 由 `EgpuRoute` 決定本趟用不用 `modeld_egpu`，條件是開關開、eGPU 在線、使用中模型的編譯檔與兩個 warp 都在、本趟未失敗。之後 USB 抖動也不會來回切。
5. `modeld_egpu`：60 秒內載入並 warmup（同 sunnypilot `BIG_MODEL_TIMEOUT`）→ 跑 dp 的主迴圈（DesireHelper、LCA、road edge、modelExt、dp 側向偏移全部沿用），`modelExt.bigModel=True`。
6. **失敗**（載入逾時或例外、輸出非有限值、執行例外）→ 設 `dp_egpu_failed`、行程結束 → manager 在下一個 loop 改起 dp 的 `modeld`，這趟不再試；熄火後清除，下次發動再試。
7. 上路時下載／編譯會中斷；已驗證的 onnx `.part` 與已完成的編譯檔都保留，熄火後接續。

---

## 5. 檔案清單（相對 Jetlink v11）

### 5.1 新增

| 路徑 | 說明 |
| --- | --- |
| `dragonpilot/egpu/__init__.py` | 模組說明 |
| `dragonpilot/egpu/constants.py` | 共用清單的參數名、tinygrad ref、快取鍵、QCOM／AMD 編譯旗標、USB ID、路徑、params、狀態常數；不 import tinygrad／capnp／hardware 層 |
| `dragonpilot/egpu/usb.py` | sysfs 偵測 eGPU |
| `dragonpilot/egpu/bundles.py` | 讀共用清單（`JetlinkCatalog`）、pointer 查詢（自己的快取＋jetlink 的）、onnx／編譯檔／warp 是否存在、每趟鎖定一次的路由 `EgpuRoute` |
| `dragonpilot/egpu/manager.py` | 常駐 `egpu_manager`：在線偵測；offroad 每小時用 `jetlink_adapter.refresh_catalog` 更新共用清單（失敗後 30 秒退避）、pointer → onnx → 編譯 → 啟用、刪除單一模型、清快取、使用中模型失效時自動重編 |
| `dragonpilot/egpu/tg_env.py` | 把隨附 tinygrad 放到 sys.path 最前面、AMD 韌體優先讀 `/lib/firmware/*.zst` |
| `dragonpilot/egpu/compile.py` | 車上編譯：`warp`（QCOM）與 `model`（eGPU；stateful 原樣，queued 包佇列） |
| `dragonpilot/egpu/runner.py` | `EgpuModelState`：QCOM warp → 單次上傳 → eGPU 模型、state aliasing、hidden state 回授、`get_action`（sunnypilot modeld_v2：有 action head 讀 action，否則 plan；gen≥11 走 mlsim；bundle 的 lat/long 平滑；保留 `dp_lat_offset_cm`） |
| `dragonpilot/egpu/modeld_egpu.py` | 行程進入點 |
| `dragonpilot/egpu/tinygrad_spm/` | 隨附 sunnypilot@9cd40014（含 LICENSE、VERSION） |
| `dragonpilot/selfdrive/ui/egpu_ui.py` | 狀態文字/顏色、圖示（沿用 jetlink 的 chestnut 圖示）、failover 說明、處理狀態文字、模型標記 |
| `dragonpilot/selfdrive/ui/layouts/settings/egpu.py` | 設定頁「eGPU」（含刪除模型） |
| `dragonpilot/settings/min-feat.dev.egpu.py`、`.yaml` | params 宣告 |
| `docs/EGPU_PORT_zh-TW.md` | 本文件 |

### 5.2 修改（皆以 jetlink v11 版為底）

| 檔案 | 修改 |
| --- | --- |
| `common/params_keys.h` | 13 個 `dp_egpu_*`（見 §6） |
| `selfdrive/modeld/modeld.py` | ① `compile_modeld` 改在 `ModelState.__init__` 內 import（egpu 行程不 import dp 的編譯工具）② `main(demo, egpu_loader=None)`：egpu 模式不建小模型、不 attach jetlink、每幀都跑、用 bundle 的平滑秒數、用 `model.get_action()`、realtime 照 sunnypilot 設 core 7 / FIFO 54。非 egpu 模式行為與 jetlink v11 完全相同 |
| `selfdrive/modeld/helpers.py` | `usbgpu_present()` 認 `ADD1:0001` 與 `3801:0001` |
| `selfdrive/modeld/SConscript` | 內建大模型 `big_driving_{vision,policy}.onnx` 不存在時跳過大模型編譯（你刪掉了它；原本插著 eGPU 建置時 `get_existing_chunks()` 會 FileNotFoundError 中斷 scons） |
| `system/manager/process_config.py` | `modeld` 改為 `only_onroad and not egpu`；新增 `NativeProcess("modeld_egpu", "dragonpilot/egpu", [sys.executable, "./modeld_egpu.py"], only_onroad and egpu)`、`PythonProcess("egpu_manager", ..., always_run)` |
| `selfdrive/selfdrived/selfdrived.py` | `egpu_manager` 不計入 processNotRunning |
| `selfdrive/ui/ui_state.py` | `EgpuState`；5 Hz 讀 `egpu_present/enabled/bundle/status/state`；eGPU 在線時 `usbgpu=True`（jetlink 因此自動隱藏，符合 jetlink「USB GPU 在時大模型歸它」的設計；mici 首頁 eGPU 圖示也會亮） |
| `selfdrive/ui/layouts/sidebar.py` | eGPU 在線：home/flag 按鈕位置顯示 chestnut 圖示（同 sunnypilot），多一張「eGPU」狀態卡 |
| `selfdrive/ui/onroad/hud_renderer.py` | 實驗模式按鈕下方的 eGPU 徽章（與 jetlink 徽章同位置，jetlink 未連線時才畫）：載入中閃爍、綠色＋模型代號＝大模型駕駛中、橘色＝未使用／失敗 |
| `selfdrive/ui/layouts/settings/settings.py` | 第 9 個面板「eGPU」 |
| `pyproject.toml` | ruff／codespell／pytest／ty 排除 `dragonpilot/egpu/tinygrad_spm` |

---

## 6. Params

| key | 型別／旗標 | 用途 |
| --- | --- | --- |
| `dp_egpu_enabled` | BOOL PERSISTENT 預設 1 | 總開關 |
| `dp_egpu_present` | BOOL CLEAR_ON_MANAGER_START | eGPU 在 USB 上（egpu_manager 每秒更新） |
| `dp_egpu_bundle` | JSON PERSISTENT | 使用中的模型（含 onnx 的 `sha256`） |
| `dp_egpu_manifest_sync` | INT CLEAR_ON_MANAGER_START | 上次更新共用清單的 monotonic ns；設 0＝要求重新整理 |
| `dp_egpu_pointers` | JSON PERSISTENT | `{ref: {oid, size}}` 已解析的 onnx pointer |
| `dp_egpu_download_ref` | STRING CLEAR_ON_MANAGER_START | 要準備（下載＋編譯）的模型；移除＝取消 |
| `dp_egpu_delete_ref` | STRING CLEAR_ON_MANAGER_START | 要刪除的模型 |
| `dp_egpu_status` | JSON CLEAR_ON_MANAGER_START | `{state, name, ref, progress, elapsed, error}` |
| `dp_egpu_clear_cache` | BOOL CLEAR_ON_MANAGER_START | 清快取 |
| `dp_egpu_auto_default` | BOOL PERSISTENT 預設 1 | 有 eGPU 時自動下載＋編譯預設模型 CTV3（只做一次；使用者選擇／取消／刪除後清除） |
| `dp_egpu_seen` | BOOL PERSISTENT 預設 0 | 曾在 USB 上看到 eGPU（含 ROM 狀態的 ASM2464）；與 `dp_egpu_enabled` 同時為真時 jetlinkd 不啟動 |
| `dp_egpu_loading` / `running` / `failed` | BOOL CLEAR_ON_MANAGER_START＋CLEAR_ON_OFFROAD_TRANSITION | 本趟大模型狀態 |

磁碟配置（`/data/media/0/models/`）：
- `egpu/warp_<相機>_512x256_tg9cd40014v1.pkl`（各約 0.5 MB）
- `egpu/model_<sha16>_tg9cd40014v1.pkl`（約等於 onnx 大小）
- `egpu/compile.log`
- `jetlink/<sha16>.onnx`（與 jetlink 共用）

---

## 7. 刻意的差異（相對 sunnypilot / Amy / dp）

1. **模型在車上編**，不下載 sunnypilot 的 pkl：那些 pkl 綁 sunnypilot 當時的 tinygrad，而且（除了 Amy 的兩個）都是整張影格經 USB。
2. **warp 是獨立的 QCOM JIT**：sunnypilot-master 也拆成兩個 JIT，但 big warp 編給 eGPU（`FRAME_DEV=CPU`）；Amy 是 fused JIT 裡把 warp 放到 QCOM。這裡取兩者之長：拆開，且 warp 在 QCOM。
3. **失敗備援改為切換行程**（同一行程不能有兩版 tinygrad）。sunnypilot 是同行程立即切小模型；這裡有 dp modeld 啟動的空窗（數秒），期間沒有 modelV2，selfdrived 會跳通訊類警示。
4. **大模型載入期間（最多 60 秒）不發布 modelV2**，與 sunnypilot 相同。
5. **只做大模型（chestnut）**；小模型仍是 dp 內建。sunnypilot 的小模型選擇器、lane turn、lagd、camera offset 不移植。
6. **realtime**：egpu 模式照 sunnypilot／新版上游設 `config_realtime_process(7, 54)`。若 3X 上 UI 卡頓，可先把 `modeld.py` 的 `if EGPU:` 那段 realtime 拿掉測試。
7. **側向偏移**：`dp_lat_offset_cm` 在 egpu 模式加在平滑**之前**（加在之後會被 bundle 的 0.1 s 平滑放大成 offset/α）。
8. **清快取會刪 jetlink 資料夾中 egpu 用過的 onnx**（使用中的除外）。eGPU 插著時 jetlink 本來就不啟用；若之後拔掉 eGPU 改用 jetlink，它會自己重新下載。
9. **未移植**：`chestnutState` 遙測（溫度、功耗，需 cereal）、chestnut 韌體燒錄、eGPU 安全退出。
10. **模型清單與 Jetlink 共用、沒有資料夾**：sunnypilot 用 TreeOptionDialog 依 bundle 的 `folder` 分組，並各自抓清單；這裡直接讀 jetlink 的 `JetlinkCatalog`，一層清單（新到舊），點選即使用；刪除改成獨立的「刪除模型」列。
11. **UI 文字直接寫繁中**（同 jetlink 面板），所有字元已確認在 `app_zh-CHT.po` 內（字型只含翻譯檔內的字，否則顯示「?」）。
12. **eGPU 辨識加入 UT3G V1.6 Dual（`ADD1:0002`，v5）**：同 sunnypilot dev-sp-egpu 的 `UT3G_DUAL_USB_IDS`。改了四處：`dragonpilot/egpu/constants.py`（`EGPU_USB_IDS`）、`selfdrive/modeld/helpers.py`（`USBGPU_IDS`）、`dragonpilot/jetlink_adapter`（`CHESTNUT_IDS`，jetlink 才不會把插 UT3G 的埠鎖成裝置端），以及**隨附 tinygrad 的 `tinygrad/runtime/ops_amd.py`**（`USBIface` 列舉 USB 裝置時多列 `ADD1:0002`；這是對 sunnypilot/tinygrad@9cd40014 唯一的修改）。沒有任何地方會對 UT3G 寫韌體。ASM2464 原廠／ROM 韌體（`174C:2464/2463`）只會記錄錯誤，不算 eGPU。
13. **預設模型自動下載（v5，方案 B）**：見 §3。sunnypilot 沒有這個功能（要使用者自己選）。
14. **有 eGPU 的機器不啟動 jetlinkd（v5.1）**：jetlink 原本的規則是「開關開著、而且沒看到 chestnut」才跑（`jetlink/openpilot/parts.py` `enabled()`）。但 jetlink 要先看到 eGPU 列舉出來才會讓開；它的 USB-C 埠邏輯（`jetlink/comma/port.py`）在 comma 當主機 3 秒（`SWAP_AFTER`）後還沒看到 chestnut 時，會把埠鎖成裝置端（comma 供電時），或對外發 DR_Swap 甚至 USB PD hard reset（對方供電時，例如外接電源的 UT3G）。UT3G 因此一直出不來，log 就顯示埠被 jetlink 佔用。v5.1 改成：eGPU 開關開，而且曾看過 eGPU（`dp_egpu_seen`，由 `egpu_manager` 寫入），`jetlink_adapter.should_run` 就回傳 False，jetlinkd 不跑；jetlinkd 停止時會先 `port.off()` 放開鎖住的埠。想改用 jetlink，關掉 eGPU 開關即可。
15. **載入／編譯的穩定性（v5.2，參考 UT3G 作者的 sp 分支與 zoompilot）**：
    - **即時排程改到載入完成後**：sunnypilot／zoompilot 都是先 `config_realtime_process(7, 54)` 再載入大模型。但隨附 tinygrad 等待 GPU 的地方（`support/hcq2.py` 的 `hcq_fence`，註解寫著「TODO: timeout?」）沒有逾時、也不讓出 CPU。GPU 不回應時，它會以 FIFO 54 在第 7 核空轉，載入逾時的檢查排不到 CPU，畫面就一直停在「載入中」也不報錯。v5.2 改成載入完才設即時排程。
    - **載入逾時 60→120 秒**：UT3G 作者在 C3XL＋UT3G 上實測載入 71.5～75.6 秒（`sunnypilot/modeld_v2/egpu_loader.py` 的 `C3XL_MODEL_LOAD_TIMEOUT = 120`）。60 秒在 UT3G 上一定會逾時。
    - **先等 PCIe 連上 L0**：比照 zoompilot `chestnut/flash.py` 的 `link_up`。橋接晶片在 USB 上出現時，後面的顯示卡不一定已經可用（各車 12 V 不同）。編譯前與載入前最多等 15 秒；送出的請求和 tinygrad 每次開啟裝置時送的完全一樣（0xF3=1、讀 0xB450），都不會寫入永久設定。
    - **`DEV=QCOM`**：同 UT3G 作者的 `configure_default_device`，讓 tinygrad 的預設裝置掃描不去碰 USB 上的 AMD GPU。
    - **`XDG_CACHE_HOME=/data/cache`**：同 UT3G 作者。C3XL 的 /home 是重開就清空的 overlay；tinygrad 在 /lib/firmware 找不到 AMD 韌體時會上網下載並快取在那裡，不改的話每次開機都要重新下載（沒網路就卡住）。
    - **編譯固定在第 7 核**：比照 zoompilot 的 `taskset -c 7`。AGNOS 把第 7 核用 isolcpus 隔離，長時間編譯時不會拖慢 UI、WiFi。
    - **對照發現**：UT3G 作者分支內附的 tinygrad，`ops_amd.py` 也只列 `ADD1:0001`／`3801:0001`，沒有 `ADD1:0002`；它的 tinygrad 也比我們的舊（有 `AMD_USB_POLL_US` 輪詢睡眠，我們這版沒有）。所以他實際能跑 UT3G 的 tinygrad，跟壓縮檔裡這份可能不同，這點無法從原始碼確認。

---

## 8. 驗證（PC，用隨附 tinygrad，DEV=CPU）

| 測試 | 結果 |
| --- | --- |
| sunnypilot-master 的 `tinygrad_repo` vs `sunnypilot/tinygrad@9cd40014` | 逐檔一致 |
| 共用清單（jetlink 的 merge 套在實際 v25、v27＋extra） | 16 個模型、新到舊排序、無重複；未更新前只有 jetlink 預設的 CTV3；manager 的重新整理確實走 `jetlink_adapter.refresh_catalog`，未過期不重抓、UI 要求時立即重抓 |
| `compile.py`：jetlink 的 tiny stateful／queued onnx（結構與 Cinque V3／舊模型相同的縮小版）＋真實尺寸 warp（1928×1208、1344×760 → 512×256） | 全部編譯成功，pickle 往返結果逐位元一致（compile_jit 內建檢查） |
| runner 端到端（12 幀、4 格相機 ring buffer） | **warp 與 numpy 參考逐位元相同**；stateful 輸出與 `tiny_model.stateful_reference` 相對誤差 2.4e-7；queued 與 jetlink `PolicyQueues`＋`tiny_model.reference` 相對誤差 5.7e-4（模型本身是 fp16，jetlink 自己的測試容許 0.02） |
| manager 實跑：pointer → jetlink `lfs_download` 從本機 HTTP 下載 → 子行程編譯 → 啟用 | 兩種模型都完成；已編譯的模型再選一次不會重編；沒插 eGPU 時停在等待、插上後自動編；刪除使用中的模型（編譯檔、onnx 都刪、改回小模型）；清快取只留使用中的模型與 warp；下載中取消：全部通過 |
| 路由鎖定 | 本趟鎖定、失敗後改 stock 直到熄火：通過 |
| ruff、py_compile、UI 字元檢查 | 新增檔案無錯誤；字元皆在字型內 |

**需要上車確認**（這個環境沒有 eGPU，也連不到 HF／GitLab）：
- 實際用 USB+AMD 編譯 766 MB／1757 MB 的 onnx 要多久、3X 只有 4 GB RAM 撐不撐得住。建議先試 CTV3M 或 BMV6 這類 766 MB 的。
- 實際的單幀時間。
- tici 畫面實際長相。

---

## 9. 安裝

```bash
# 前提：/data/openpilot 已是 dp0111pre（2026-10-14）＋ jetlink v11
cd /tmp && unzip -o openpilot-dp0111pre-egpu-v2.zip && cp -a openpilot-dp0111pre-egpu-v2/. /data/openpilot/
# 若之前裝過 v1：刪掉 v1 的隨附 tinygrad 與檔案
rm -rf /data/openpilot/dragonpilot/egpu/tinygrad_sp /data/openpilot/dragonpilot/egpu/chunked.py
sudo reboot
```

（git 樹：`git apply egpu-port.patch`，再另外複製 zip 內的 `dragonpilot/egpu/tinygrad_spm/`。patch 不含那 18 MB。）

v1 留下的東西：v1 的 `dp_egpu_bundle` 沒有 `sha256`，會被自動視為無效；v1 下載的 Amy pkl 在 `/data/media/0/models/egpu/`，按一次「清除模型快取」就會刪掉。

**上車檢查**：
- `cat /data/params/d/dp_egpu_present` → `1`
- 設定 → eGPU → 選 Cinque Terre V3 → 下載並編譯，看「處理狀態」；編譯細節在 `/data/media/0/models/egpu/compile.log`
- 發動後 log 應有 `egpu: layout=stateful model on AMD, warp on QCOM, new_img (2, 6, 128, 256)` 與 `loaded in …s`
- 行車畫面右上：綠色 chestnut 圖示＋`CTV3M`

**疑難排解**：
- 編譯失敗：看 `compile.log` 最後的錯誤。
- 一直「失敗」：看 log 的 `egpu: big model failed: …`。`wrong tinygrad` → `tinygrad_spm` 沒覆蓋到；`timeout 60s` → 把 `modeld_egpu.py` 的 `BIG_MODEL_TIMEOUT` 調大。
- AMD 韌體：`tg_env.patch_fetch_fw()` 先找 `/lib/firmware/<path>/<name>.zst`，找不到才上網抓（第一次編譯時有網路即可）。
