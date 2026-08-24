# SARILS Video Edit Engine v0.3

가이드 영상 기반 숏폼 자동 편집 엔진. **LLM은 엔진 밖**에 있고, 엔진은 검증된
구조화 JSON(EditRecipe)만 받아 MP4와 Manifest를 만든다.

```
[엔진 밖]  Gemini 가이드 분석 · GPT READ-2 · Orchestrator
             ↕ 구조화 JSON
[엔진 안]  CUT_ASSEMBLY  : 모션·품질·의미 분석 → 앞뒤 트림 → 순서 보존 결합
           FINAL_RENDER  : Validator → Avoid Map → 자막 배치 → FFmpeg → QC
```

## 설치 — 노트북(Windows + RTX 4090 Laptop 16GB)

**경로 A · WSL2 (권장 — 검증된 리눅스 환경 그대로)**

```powershell
wsl --install -d Ubuntu-24.04   # PowerShell(관리자), 재부팅 후 계정 생성
```
```bash
# Ubuntu 안에서
sudo apt update && sudo apt install -y ffmpeg python3-venv unzip
unzip sarils-video-engine-v0.3.1.zip && cd sarils-video-engine
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python tools/fetch_models.py                    # Pretendard + MediaPipe 모델
python tools/verify_gpu.py                       # 1차: CPU 스택 확인
pip uninstall -y onnxruntime
pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements-gpu.txt
hf auth login                                  # SAM 3.1 승인 계정으로 기기 로그인
python tools/verify_gpu.py --video 내영상.mp4     # 2차: GPU 스택 확인
python demo/run_quality_max.py --video 내영상.mp4 # 품질 최대 렌더
```
주의: WSL2는 CUDA만 지원하고 **NVENC은 미지원** → 인코딩만 libx264(CPU)로
자동 전환된다(13900H면 충분히 빠름). AI 추론은 전부 GPU.

**경로 B · Windows 네이티브** — NVENC까지 쓰려면 이쪽:
winget으로 `Gyan.FFmpeg`(full)과 Python 3.11+ 설치 후 위와 동일한 pip 순서
(venv 활성화는 `.venv\Scripts\activate`). 엔진은 Windows 경로·RAM 체크를
지원하도록 처리되어 있다.

## 설치 — CPU (개발·CI)

```bash
pip install -r requirements.txt
python tools/fetch_models.py
python tools/verify_gpu.py

# RTX 4090 Laptop (16GB) — requirements-gpu.txt 상단 주의사항 필독
pip uninstall -y onnxruntime
pip install -r requirements-gpu.txt
hf auth login                                  # facebook/sam3.1 사용 승인 필요
python tools/verify_gpu.py --video sample.mp4
```

CUDA가 감지되면 코드 변경 없이 자동 승격된다.

Hugging Face 토큰은 소스나 `.env`에 저장하지 않는다. `hf auth login`의 기기
로그인을 사용하며, 체크포인트는 프로젝트 밖의 사용자 캐시에서 관리한다.
Pretendard 1.3.9와 MediaPipe 모델도 저장소에 넣지 않고
`tools/fetch_models.py`가 고정 URL과 체크섬을 사용해 준비한다.

| 슬롯 | CPU | RTX 4090 |
|---|---|---|
| 세그멘테이션 | MediaPipe selfie_multiclass | **SAM 3.1** (개념 프롬프트) |
| 객체·상품 | EfficientDet-Lite2 | **YOLO11-L** |
| 얼굴 | MediaPipe BlazeFace | 동일 (GPU delegate) |
| 화면 글자 | PP-OCR ONNX (CPU EP) | PP-OCR ONNX (**CUDA EP**) |
| READ-1 의미 | OpenCV 모션·품질 (conf≤0.79 상한) | **Qwen3-VL 8B-AWQ/4B** ↔ Pegasus 폴백 |
| 인코딩 | libx264 | **h264_nvenc** |

`SARILS_FORCE_CPU=1`로 GPU를 강제로 끌 수 있다.

## VRAM 예산 (16GB — RTX 4090 Laptop)

데스크톱 4090(24GB)이 아니라 **Laptop 16GB**다. 8B bf16 VLM(≈17GB)은 로드
불가 — AWQ int4 또는 4B 필수. 단계별 lazy load → `close()`, 동시 상주 금지.

```
CUT_ASSEMBLY : Qwen3-VL 8B-AWQ (~9-10GB) → close
FINAL_RENDER : SAM 3.1 (~4GB) + YOLO (~2GB) + PP-OCR (~1GB) → close → NVENC
```

전력 제한(≈120-150W)으로 지속 부하 성능은 데스크톱의 60-70% 수준 — 배치가
길면 스로틀을 감안해 `frames_per_window`를 4~6에서 조절한다.

## 구조

```
sarils_edit_engine/
  contracts.py        Pydantic 계약 (EditRecipe / CutManifest / RenderManifest / QC)
  registries.py       Effect·Font·SafeArea·RenderProfile·AudioMix 로더 + 해시 검증
  validator.py        렌더 전 정책 검증 (순서·폰트 글리프·SFX·범위)
  subtitle_layout.py  실폰트 메트릭 배치 + ASS 생성
  ffmpeg_graph.py     filter graph 빌더 (libx264 / h264_nvenc 분기)
  cut_assembly.py     READ-1 순서 보존 결합
  qc.py               Post-render QC
  engine.py           파사드 (idempotency · 폴백)
  model_adapters/
    device.py         CUDA 감지 · ONNX provider · VRAM 해제
    frames.py         프레임 샘플링 (preview 해상도 → 좌표 역변환)
    quality.py        OpenCV 모션·선명도·흔들림 → 대기구간·신뢰도
    vision.py         MediaPipe Face/Pose/Selfie/EfficientDet + PP-OCR
    vision_gpu.py     SAM 3.1 · YOLO (CUDA 전용, 실패 시 폴백)
    semantic.py       Motion / Qwen3-VL / Pegasus + 하이브리드 라우터
    avoid_map.py      어댑터 병합 → AvoidMap (IoU 머지)
```

## 실행

```bash
python demo/run_gpu_stack.py --video sample.mp4 # 입력 준비 + 실제 GPU E2E
python demo/run_a_only.py        # ONE_TAKE + SFX_ONLY
python demo/run_d_only.py        # SFX Provider 장애 → SILENT fallback
```

## 검증된 SAM 3.1 기준

- GPU: NVIDIA GeForce RTX 4090 Laptop GPU 16GB
- 모델 로드: 약 9.9초, 추론 최대 VRAM 약 5.1GB
- `person` 프롬프트 실영상 분할 및 엔진 폴백 없는 통합 실행 통과
- 최종 1080x1920 H.264/AAC 렌더와 Post-render QC 11개 통과
- 검증 조합: PyTorch 2.10.0+cu128, ONNX Runtime GPU 1.26.0,
  SAM 3 커밋 `8f0b7f4d4e7eda2ed606ebde6702c93359ad01da`

## 정책 (변경 금지)

- 컷 **재배열 금지** — 순서는 가이드+촬영 순서로 이미 결정. 엔진 권한은 in/out 트림뿐
- 의미 매핑 없는 모션 단독 판정은 confidence **0.79 상한** — 자동 트림 티어 진입 불가
- 촬영 원음 **REMOVE**, BGM **NONE**, 최종 오디오는 `SILENT` 또는 `SFX_ONLY`
- 자막 폰트는 **Pretendard 고정**. 파일 누락·해시 불일치·미지원 글리프면 **렌더 차단**
- Post-render QC FAIL이면 `deliverable=false` — 사용자에게 전달하지 않는다
