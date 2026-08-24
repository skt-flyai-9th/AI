# SARILS Codex Project Instructions

## Project identity

- Service name: **사릴스 (SARILS)**.
- This repository (`skt-flyai-9th/AI`) is the AI service and is the Codex working root.
- Codex integration branch: `codex/sarils-project`.
- Default upstream branch: `main`.
- Local/Codespaces path used for this repository: `/workspaces/AI`.
- Run the AI service with:
  - `uvicorn app.main:app --host 0.0.0.0 --port 8001`

## ChatGPT `민륜기` project sync policy

This file is the coding-facing mirror of the confirmed SARILS decisions made in the ChatGPT project named **`민륜기`**.

- Treat confirmed product/architecture decisions from `민륜기` as input that should be mirrored into this `AGENTS.md` before or together with implementation work.
- When a confirmed decision changes an existing rule, update this file so Codex does not continue from stale context.
- Do not copy temporary brainstorming, discarded alternatives, or unresolved ideas into this file as binding rules.
- Only decisions that are confirmed enough to affect implementation, API contracts, Agent behavior, data/schema rules, integrations, UX state behavior, or hard product constraints should become persistent Codex instructions.
- If ChatGPT context and this file diverge, the newest explicit user decision in `민륜기` wins and this file should be updated to match it.
- Codex should use this repository's code/tests and this file together; this file supplies product intent and cross-repository constraints, while code/tests supply the currently implemented contract.
- This synchronization is repository-based, not automatic shared-memory synchronization between ChatGPT Projects and Codex. Keep the mirror current through committed updates to this file.

Operationally, use this loop:

`민륜기 decision -> update AGENTS.md -> Codex implementation -> tests/docs -> commit/PR`

## Repository topology

The overall product is split across three repositories:

1. AI: `skt-flyai-9th/AI` (`main`) — this repository, FastAPI AI service.
2. Backend: `skt-flyai-9th/backend` (`develop`) — main application backend.
3. Frontend: `skt-flyai-9th/frontend` (`main`) — user-facing frontend.

Runtime direction:

`Frontend -> Backend (:8000) -> AI (:8001)`

The AI service is not called directly by the frontend. Backend-to-AI calls use the internal API contract and `X-Internal-API-Key`. Treat backend/frontend as integration targets and do not assume write access to those repositories from this Codex project.

## Existing AI server behavior that must not be broken

- The AI service is deployed separately from the main backend.
- The currently implemented/available agent in this repository is `challenge-ranking`.
- Challenge ranking currently uses an Instagram/Apify -> Gemini -> NAVER API HUB -> YouTube pipeline and exposes FastAPI JSON APIs.
- Existing ranking-run/status/result/current-result APIs and their response contracts should remain backward compatible unless the task explicitly changes them.
- Keep secrets in environment/configuration; never hard-code API keys.

## Canonical product agent names

Use these names consistently in new architecture, LangGraph, docs, prompts, and code:

- **숏폼 Agent**
- **트렌드 리서치 Agent**
- **편집 Agent**

Do not use the old names `Shortform Director Agent`, `Trend Research Agent`, or `Editing Director Agent` in new implementation work.

## Product-wide hard constraint

- **TTS is completely excluded from SARILS.**
- Do not add speech synthesis, TTS providers, TTS configuration, narration generation, or TTS fallback paths.

## Store onboarding context

User-entered onboarding information:

- 매장 분위기
- 매장 사진
- 대표 컬러
- 대표 메뉴/가격

Naver Map/store lookup only auto-collects:

- 업종
- 위치

Do **not** assume the full menu list is automatically imported from Naver. Commercial-area data is separately used for store context, including trade-area characteristics and target age information.

If a project promotes a specific menu, new menu, seasonal menu, multiple menus, product, or service beyond the representative menu, the user must provide the actual promotion target during project creation.

## Minimum project-creation input

Required project input is exactly:

- `store_id`
- 홍보 목적
- 홍보 대상 메뉴·상품·서비스
- 촬영 가능 시간
- 얼굴 노출 가능 여부

Do not make the following required project inputs:

- 촬영 인원
- 사용 가능한 공간
- 사용 가능한 소품
- 원하는 분위기
- 추가 제약
- 기존 사진·영상 사용 여부

## Short-form recommendation rules

The **숏폼 Agent** must recommend from **ACTIVE 영상편집템플릿 only**.

Flow:

1. Code performs hard-condition filtering first.
2. Candidate pool contains ACTIVE 영상편집템플릿 only.
3. Use conversation-confirmed information + Store Context + promotion target/purpose + available filming time + face-exposure condition + Trend Context.
4. LLM ranks the remaining candidates and chooses exactly one template.
5. The LLM must **not freely invent a new short-form concept that does not exist in the template library**.

Hard conditions such as face-exposure prohibition and product/feature impossibilities must be filtered in code before LLM selection.

If hard filtering leaves zero candidates:

- Do not terminate as “no recommendation”.
- Minimally relax only user-confirmed conditions that are actually soft/relaxable.
- Never relax safety constraints, product hard limits, or functionally impossible conditions.
- Return the nearest ACTIVE template.

## Recommendation UI/session behavior

- Show exactly **one** short-form candidate at a time.
- `다시 추천 받기`:
  - keep `store_id`, Store Context, current project brief, confirmed conditions, and conversation context;
  - do not ask for a rejection reason;
  - exclude all templates already shown in this recommendation session;
  - recommend one next ACTIVE template from the remaining pool.
- `새로고침`:
  - reset the entire current recommendation session;
  - clear project state, conversation state, recommendation history, and confirmed conditions;
  - reset selected `store_id` and Store Context as part of the session reset;
  - restart from store selection/entry.
- Accepting a recommendation shows the selected 영상편집템플릿's 촬영 가이드.

## Editing input constraints

The editing model operates on user-recorded/uploaded **video clips**.

It does **not** support:

- adding still photos onto the video timeline;
- turning still photos into video scenes.

Store photos may be used as store-analysis/context reference only, not as editing source media.

## Template Knowledge Manager

상권분석템플릿 and 영상편집템플릿 are shared platform knowledge assets and must be managed by one **Template Knowledge Manager** lifecycle.

Rules:

- both template types are versioned and updated periodically;
- never overwrite the current template in place;
- generate an update candidate;
- compare via diff;
- validate against policy/schema/product constraints;
- require human approval when configured/needed;
- save a new version after validation;
- expose only the intended ACTIVE version(s) to recommendation logic.

상권분석템플릿 update:

- compare existing template with newer trade-area/industry evidence;
- use GPT to generate update candidates.

영상편집템플릿 update:

- Trend Research output is a prerequisite input;
- use the YouTube reference links in Trend Research;
- Gemini analyzes those videos and writes editing insights;
- use those insights to generate the template update candidate.

## Trend discovery / challenge research architecture

Use this responsibility split:

- **Instagram/Apify**: discover challenge candidates and verify real participation/spread.
- **YouTube**: verify cross-platform spread and provide final representative video links.
- **NAVER API HUB**: verify Korean relevance/trend using search/blog/news signals.
- **Gemini**: cluster semantically equivalent challenge expressions/audio/hashtags/video patterns and remove false positives.

Recommended Apify rollout:

1. Start with Instagram Search Scraper `popular reels` using seed keywords.
2. Expand discovered hashtag/audio terms automatically and search again.
3. When needed, use Instagram Hashtag Scraper and Reel Scraper to validate unique creator count, repeated audio, recent post growth, and behavioral similarity.
4. First A/B compare Instagram-led discovery against the existing YouTube-led result before expanding the deeper analysis stages.

Important caveats:

- Apify/scraping is not an official Meta Trend API.
- Seed keywords are still needed.
- Scraped fields can be unstable.
- Korean audience geo is not guaranteed by Instagram alone; combine Korean caption/ASR/hashtag, creator/location, NAVER, and YouTube signals.
- Review Meta automated-collection terms and commercial-use risk separately.
- Implement Instagram as a replaceable `Instagram Source Adapter` so an authorized provider or official access method can replace Apify later.

## Representative YouTube video selection

The representative video is **not** simply the most viewed video. Its purpose is to let a user click once and quickly understand the actual Korean-trending challenge through a genuine participation video.

Before scoring, filter out:

- videos below the minimum challenge-relevance threshold;
- commentary/reaction/explanation-only videos;
- unrelated videos.

Then use a weighted score:

- 챌린지 관련성: **40%**
- 실제 참여/따라하기 여부: **20%**
- 조회 성과: **15%**
- 최신성: **10%**
- 참여율: **5%**
- 국내 관련성: **10%**

YouTube public APIs do not expose a normal-video `shareCount`, so engagement should be computed mainly from `likeCount` and `commentCount` rather than an assumed share metric.

## Engineering expectations for Codex

- Read the existing code and tests before changing architecture.
- Preserve existing public/internal API contracts unless the requested task explicitly changes them.
- Prefer explicit schemas/types for Agent state, Store Context, Project Brief, Trend Context, template versions, and API payloads.
- Put hard constraints in deterministic code, not prompt text alone.
- Keep LLM responsibilities focused on ranking, interpretation, clustering, or structured generation after deterministic validation/filtering.
- Add/update tests for every behavior change, especially session reset/recommend-again behavior, template eligibility filtering, hard-vs-soft constraint handling, and API contracts.
- Update repository docs when implementation changes behavior or integration contracts.
- Do not silently invent missing backend/frontend contracts. Verify them from their repositories or existing integration docs first.

## Source-of-truth priority

When instructions conflict, use this priority:

1. Explicit current user request.
2. This `AGENTS.md` product constraints.
3. Existing API contracts/tests that have not been explicitly superseded.
4. Repository documentation.
5. Existing implementation details.

If a requested change conflicts with a hard product constraint above, surface the conflict instead of silently implementing around it.
