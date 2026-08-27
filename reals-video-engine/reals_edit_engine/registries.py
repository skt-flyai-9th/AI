"""Registry 로더 — 폰트 해시 검증 실패 시 렌더 차단 (구현 문서 2.7·18.1)."""
from __future__ import annotations
import hashlib, json, pathlib

ENGINE_VERSION = "reals-edit-engine/0.3.5"


class RegistryError(Exception):
    """자산·참조 불일치 — bundle 활성화/렌더 차단 사유."""


class Registries:
    def __init__(self, root: str | pathlib.Path):
        self.root = pathlib.Path(root)
        reg = self.root / "registry"
        self.font = self._load(reg / "font_registry.json")
        self.effect = self._load(reg / "effect_registry.json")
        self.safe_area = self._load(reg / "safe_area_profiles.json")
        self.render_profiles = self._load(reg / "render_profiles.json")
        self.audio_mix = self._load(reg / "audio_mix_policy.json")
        self.edit_policies = self._load(reg / "edit_policies.json")
        self.manifest = self._load(reg / "manifest.json")

    @staticmethod
    def _load(p: pathlib.Path) -> dict:
        if not p.exists():
            raise RegistryError(f"registry 파일 없음: {p}")
        return json.loads(p.read_text())

    # ── 폰트 ──
    def resolve_font(self, font_asset_id: str, weight: str) -> dict:
        fonts = self.font.get("fonts", {})
        if font_asset_id not in fonts:
            raise RegistryError(f"미등록 폰트: {font_asset_id}")
        entry = fonts[font_asset_id]
        if weight not in entry["files"]:
            raise RegistryError(f"{font_asset_id}에 없는 weight: {weight}")
        f = entry["files"][weight]
        path = self.root / f["path"]
        if not path.exists():
            raise RegistryError(f"폰트 파일 누락 — 렌더 차단: {f['path']}")
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != f["sha256"]:
            raise RegistryError(f"폰트 해시 불일치 — 렌더 차단: {f['path']}")
        return {**f, "abs_path": str(path), "fonts_dir": str(path.parent)}

    # ── 효과 ──
    def validate_effect(self, effect_id: str, params: dict) -> None:
        effects = self.effect.get("effects", {})
        if effect_id not in effects:
            raise RegistryError(f"미등록 effect: {effect_id}")
        allowed = effects[effect_id]["allowed_params"]
        for k, v in params.items():
            if k not in allowed:
                raise RegistryError(f"{effect_id}: 허용되지 않은 param {k}")
            rule = allowed[k]
            if "enum" in rule and v not in rule["enum"]:
                raise RegistryError(f"{effect_id}.{k}: {v} not in {rule['enum']}")
            if "min" in rule and (v < rule["min"] or v > rule["max"]):
                raise RegistryError(f"{effect_id}.{k}={v} 범위 밖 [{rule['min']},{rule['max']}]")

    def style_ids_for(self, overlay_type: str) -> list[str]:
        return self.effect.get("overlay_types", {}).get(overlay_type, {}).get("style_ids", [])

    def motion_ids_for(self, overlay_type: str) -> list[str]:
        return self.effect.get("overlay_types", {}).get(overlay_type, {}).get("motion_ids", [])

    def sfx_intent_ids(self) -> list[str]:
        return self.effect.get("overlay_types", {}).get("SFX", {}).get("intent_ids", [])

    # ── 프로필 ──
    def safe_area_profile(self, pid: str) -> dict:
        try:
            return self.safe_area["profiles"][pid]
        except KeyError:
            raise RegistryError(f"미등록 safe area profile: {pid}")

    def render_profile(self, pid: str) -> dict:
        try:
            return self.render_profiles["profiles"][pid]
        except KeyError:
            raise RegistryError(f"미등록 render profile: {pid}")

    def audio_policy(self, pid: str) -> dict:
        try:
            return self.audio_mix["policies"][pid]
        except KeyError:
            raise RegistryError(f"미등록 audio mix policy: {pid}")

    def versions(self) -> dict:
        return {
            "edit_engine_version": ENGINE_VERSION,
            "template_bundle_id": self.manifest.get("template_bundle_id"),
            "template_version": self.manifest.get("template_version"),
            "effect_registry_version": self.effect.get("effect_registry_version"),
            "font_registry_version": self.font.get("font_registry_version"),
            "edit_policy_version": self.edit_policies.get("edit_policy_version"),
        }
