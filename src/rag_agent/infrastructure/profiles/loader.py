"""Carga de perfiles desde YAML.

Un perfil es la única pieza que cambia entre un RAG de credenciales y uno de
fichas técnicas, así que tenía que ser editable sin tocar Python. El YAML se
traduce a un objeto de dominio inmutable y a un *enlace de despliegue* —qué
Knowledge Base y qué carpetas le corresponden—, que son cosas distintas y por
eso viajan separadas: cambiar una frase del prompt no debería obligar a
redesplegar, y cambiar de KB no debería tocar las reglas.

Las claves desconocidas son un error, no un aviso. Un `top_k: 12` escrito como
`topk: 12` se ignoraría en silencio y el operador tardaría una tarde en
descubrir por qué su ajuste no hace nada.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.profile import ChunkPolicy, Profile, RetrievalPolicy
from ...domain.redaction import RedactionPolicy

_CLAVES = {
    "slug", "name", "subject", "sources", "decline_phrase", "extra_rules",
    "redaction", "retrieval", "chunking", "path_metadata", "banned_markers",
    "corpus", "knowledge_base_id",
}
_CLAVES_CORPUS = {"source", "prepared"}


class ProfileError(ValueError):
    """El YAML de un perfil no describe un perfil válido."""


@dataclass(frozen=True)
class ProfileBinding:
    """Perfil + dónde viven sus datos en este despliegue concreto."""

    profile: Profile
    knowledge_base_id: str | None = None
    # Carpeta con los documentos originales (PDF, XML, JSON).
    source_dir: str | None = None
    # Carpeta con el corpus ya preparado (Markdown + .metadata.json). Es lo
    # único que se sube a S3 y lo que lee la recuperación local.
    prepared_dir: str | None = None

    @property
    def slug(self) -> str:
        return self.profile.slug


def _exigir(condicion: bool, mensaje: str) -> None:
    if not condicion:
        raise ProfileError(mensaje)


def _tramo(datos: dict[str, Any], clave: str, permitidas: set[str], origen: str) -> dict[str, Any]:
    valor = datos.get(clave) or {}
    _exigir(isinstance(valor, dict), f"{origen}: '{clave}' debe ser un mapa")
    sobrantes = set(valor) - permitidas
    _exigir(not sobrantes, f"{origen}: claves desconocidas en '{clave}': {sorted(sobrantes)}")
    return valor


def parse_profile(datos: dict[str, Any], *, origen: str = "<memoria>") -> ProfileBinding:
    _exigir(isinstance(datos, dict), f"{origen}: el perfil debe ser un mapa YAML")
    sobrantes = set(datos) - _CLAVES
    _exigir(not sobrantes, f"{origen}: claves desconocidas: {sorted(sobrantes)}. Válidas: {sorted(_CLAVES)}")

    slug = str(datos.get("slug") or "").strip()
    _exigir(bool(slug), f"{origen}: falta 'slug'")

    retrieval = _tramo(datos, "retrieval", {"top_k", "min_score"}, origen)
    chunking = _tramo(datos, "chunking", {"max_chars", "overlap_chars", "min_chars_to_split"}, origen)
    corpus = _tramo(datos, "corpus", _CLAVES_CORPUS, origen)

    redaction = datos.get("redaction") or []
    _exigir(isinstance(redaction, list), f"{origen}: 'redaction' debe ser una lista de patrones")

    try:
        profile = Profile(
            slug=slug,
            name=str(datos.get("name") or slug),
            subject=str(datos.get("subject") or "").strip(),
            sources=str(datos.get("sources") or "los documentos disponibles").strip(),
            decline_phrase=str(
                datos.get("decline_phrase") or Profile.__dataclass_fields__["decline_phrase"].default
            ).strip(),
            extra_rules=tuple(str(r).strip() for r in (datos.get("extra_rules") or [])),
            redaction=RedactionPolicy(tuple(str(n) for n in redaction)),
            retrieval=RetrievalPolicy(**retrieval),
            chunking=ChunkPolicy(**chunking),
            path_metadata=tuple(str(p) for p in (datos.get("path_metadata") or [])),
            banned_markers=tuple(str(m) for m in (datos.get("banned_markers") or [])),
        )
    except (ValueError, TypeError) as exc:
        raise ProfileError(f"{origen}: {exc}") from exc

    return ProfileBinding(
        profile=profile,
        knowledge_base_id=(datos.get("knowledge_base_id") or None),
        source_dir=(corpus.get("source") or None),
        prepared_dir=(corpus.get("prepared") or None),
    )


def load_profile(ruta: Path) -> ProfileBinding:
    import yaml

    try:
        datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"{ruta.name}: YAML inválido — {exc}") from exc
    return parse_profile(datos or {}, origen=ruta.name)


def load_profiles(directorio: Path | str) -> dict[str, ProfileBinding]:
    """Todos los perfiles de una carpeta, indexados por slug.

    Se ordena por nombre de archivo para que el «primero» sea determinista: es
    el que se toma como perfil por defecto cuando no se declara ninguno.
    """
    carpeta = Path(directorio)
    if not carpeta.is_dir():
        return {}
    bindings: dict[str, ProfileBinding] = {}
    for ruta in sorted(carpeta.glob("*.y*ml")):
        binding = load_profile(ruta)
        _exigir(
            binding.slug not in bindings,
            f"{ruta.name}: el slug '{binding.slug}' ya está definido en otro archivo",
        )
        bindings[binding.slug] = binding
    return bindings
