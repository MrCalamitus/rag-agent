"""Perfiles: el tema como dato, no como código.

Un perfil decide las reglas del prompt, cuánta evidencia se recupera y qué se
enmascara. Que sea configuración y no código es lo que permite reutilizar el
agente en un tema nuevo sin tocar Python — y lo que obliga a validarlo con la
misma dureza que se valida una petición.
"""

from __future__ import annotations

import pytest

from rag_agent.domain.errors import AgentError, ErrorType
from rag_agent.domain.profile import Profile
from rag_agent.domain.prompts import build_system_prompt
from rag_agent.domain.redaction import RedactionPolicy
from rag_agent.infrastructure.profiles import (
    ProfileBinding,
    ProfileError,
    StaticProfileRegistry,
    load_profiles,
    parse_profile,
)

MINIMO = {"slug": "coches", "name": "Coches", "subject": "los vehículos documentados"}


def _binding(slug: str, **kwargs) -> ProfileBinding:
    return ProfileBinding(profile=Profile(slug=slug, name=slug, subject="algo", **kwargs))


# --- carga --------------------------------------------------------------------


def test_un_perfil_minimo_se_carga_con_valores_por_defecto_seguros():
    """Lo que no se declara no debe activarse solo: sin redacción, sin vetos."""
    binding = parse_profile(MINIMO)

    assert binding.slug == "coches"
    assert binding.profile.redaction.nombres == ()
    assert binding.profile.banned_markers == ()
    assert binding.profile.retrieval.top_k == 6


def test_una_clave_mal_escrita_es_un_error_y_no_un_ajuste_ignorado():
    """`topk` en vez de `top_k` se ignoraría en silencio y costaría una tarde."""
    with pytest.raises(ProfileError, match="desconocidas"):
        parse_profile({**MINIMO, "topk": 12})

    with pytest.raises(ProfileError, match="desconocidas"):
        parse_profile({**MINIMO, "retrieval": {"top_k": 8, "minscore": 0.3}})


def test_un_patron_de_redaccion_inexistente_se_rechaza():
    with pytest.raises(ProfileError):
        parse_profile({**MINIMO, "redaction": ["numero_de_serie"]})


def test_un_perfil_sin_slug_o_sin_tema_no_se_carga():
    with pytest.raises(ProfileError):
        parse_profile({"name": "Sin slug", "subject": "algo"})
    with pytest.raises(ProfileError):
        parse_profile({"slug": "x", "name": "Sin tema"})


def test_los_perfiles_del_repositorio_son_validos(tmp_path):
    """Guarda contra editar un YAML y romper el arranque sin enterarse."""
    perfiles = load_profiles("profiles")

    assert {"luis-cv", "autos"} <= set(perfiles)
    assert perfiles["luis-cv"].profile.masks_identifiers
    assert not perfiles["autos"].profile.masks_identifiers


def test_una_carpeta_sin_perfiles_devuelve_un_mapa_vacio(tmp_path):
    assert load_profiles(tmp_path) == {}


# --- registro -----------------------------------------------------------------


def test_el_registro_resuelve_el_slug_y_cae_al_por_defecto():
    registro = StaticProfileRegistry(
        {"coches": _binding("coches"), "inversiones": _binding("inversiones")},
        default_slug="inversiones",
    )

    assert registro.resolve("coches").slug == "coches"
    assert registro.resolve(None).slug == "inversiones"
    assert registro.slugs() == ("coches", "inversiones")


def test_un_tema_inexistente_es_un_error_de_cliente_que_enumera_los_validos():
    """Con un servicio sirviendo varios temas, el slug mal escrito es el error
    más probable: la respuesta tiene que decir cuáles hay."""
    registro = StaticProfileRegistry({"coches": _binding("coches")})

    with pytest.raises(AgentError) as fallo:
        registro.resolve("coche")

    assert fallo.value.type is ErrorType.INVALID_REQUEST
    assert fallo.value.code == "profile_not_found"
    assert "coches" in fallo.value.message


def test_un_despliegue_sin_perfiles_no_puede_construirse():
    with pytest.raises(ProfileError):
        StaticProfileRegistry({})


def test_un_por_defecto_inexistente_falla_al_arrancar_y_no_en_la_primera_peticion():
    with pytest.raises(ProfileError):
        StaticProfileRegistry({"coches": _binding("coches")}, default_slug="inversiones")


def test_los_ids_de_knowledge_base_se_inyectan_sin_tocar_el_yaml():
    """Los IDs son un hecho del despliegue: el mismo YAML sirve en local y en
    producción precisamente porque no los contiene."""
    registro = StaticProfileRegistry({"coches": _binding("coches")})

    fusionado = registro.with_knowledge_base_ids({"coches": "KB123"})

    assert fusionado.binding("coches").knowledge_base_id == "KB123"
    assert registro.binding("coches").knowledge_base_id is None


def test_declarar_una_kb_para_un_tema_inexistente_es_un_error():
    registro = StaticProfileRegistry({"coches": _binding("coches")})

    with pytest.raises(ProfileError):
        registro.with_knowledge_base_ids({"inversiones": "KB999"})


# --- efecto en el prompt ------------------------------------------------------


def test_el_perfil_manda_en_el_prompt():
    from rag_agent.domain.conversation import Conversation, Role, Turn

    perfil = Profile(
        slug="coches",
        name="Coches",
        subject="las fichas técnicas de coches",
        sources="folletos oficiales",
        decline_phrase="No aparece en la documentación.",
        extra_rules=("Indica siempre la versión a la que corresponde la cifra.",),
    )
    conversacion = Conversation((Turn(Role.USER, "¿Cuánta potencia tiene?"),))

    prompt = build_system_prompt(conversacion, (), profile=perfil)

    assert "las fichas técnicas de coches" in prompt
    assert "folletos oficiales" in prompt
    assert "No aparece en la documentación." in prompt
    assert "Indica siempre la versión" in prompt


def test_la_regla_de_enmascarado_solo_aparece_si_el_perfil_enmascara():
    """En un corpus técnico esa regla sobra y confunde: no hay identificadores."""
    from rag_agent.domain.conversation import Conversation, Role, Turn

    conversacion = Conversation((Turn(Role.USER, "¿Y?"),))
    sin_pii = Profile(slug="a", name="a", subject="fichas")
    con_pii = Profile(slug="b", name="b", subject="credenciales", redaction=RedactionPolicy.mexicana())

    assert "identificadores completos" not in build_system_prompt(conversacion, (), profile=sin_pii)
    assert "identificadores completos" in build_system_prompt(conversacion, (), profile=con_pii)


def test_las_reglas_del_perfil_van_despues_de_las_innegociables():
    """Una regla de tema puede añadir postura; nunca relajar el fundamento."""
    from rag_agent.domain.prompts import build_rules

    perfil = Profile(slug="a", name="a", subject="x", extra_rules=("Regla propia del tema.",))

    reglas = build_rules(perfil)

    assert reglas.index("Responde SOLO con lo que aparezca") < reglas.index("Regla propia del tema.")


# --- arranque -----------------------------------------------------------------


def test_en_local_sin_perfiles_se_sintetiza_uno_generico(tmp_path):
    """Quien acaba de clonar debe poder hacer `make run` sin configurar nada."""
    from rag_agent.infrastructure.config import Settings
    from rag_agent.infrastructure.container import build_profiles

    registro = build_profiles(Settings(profiles_dir=str(tmp_path), environment="local", _env_file=None))

    assert registro.default.slug == "generico"


def test_en_un_despliegue_sin_perfiles_el_arranque_falla(tmp_path):
    """Arrancar con el genérico en producción sería contestar 200 con reglas que
    nadie escribió, sobre un corpus que no es el suyo."""
    from rag_agent.infrastructure.config import Settings
    from rag_agent.infrastructure.container import build_profiles

    with pytest.raises(ProfileError, match="perfil"):
        build_profiles(Settings(profiles_dir=str(tmp_path), environment="prod", _env_file=None))


def test_un_corpus_explicito_gana_al_declarado_por_el_perfil(tmp_path):
    """Apuntar el servicio a un corpus concreto —una evaluación, reproducir un
    fallo— no debe exigir editar el YAML de cada tema."""
    from rag_agent.infrastructure.config import Settings
    from rag_agent.infrastructure.container import build_knowledge_bases, build_profiles

    (tmp_path / "explicito").mkdir()
    (tmp_path / "explicito" / "doc.md").write_text("Contenido explícito.", encoding="utf-8")
    ajustes = Settings(
        profiles_dir="profiles", default_profile="luis-cv",
        corpus_dir=str(tmp_path / "explicito"), environment="local", _env_file=None,
    )
    perfiles = build_profiles(ajustes)

    kb = build_knowledge_bases(ajustes, perfiles).for_profile(perfiles.default)

    assert kb._dir == tmp_path / "explicito"


def test_las_rutas_del_perfil_expanden_la_virgulilla():
    """Sin expandir, `~/corpus-x` busca una carpeta llamada literalmente «~»."""
    from rag_agent.infrastructure.config import Settings
    from rag_agent.infrastructure.container import build_knowledge_bases, build_profiles

    ajustes = Settings(
        profiles_dir="profiles", default_profile="luis-cv", environment="local", _env_file=None
    )
    perfiles = build_profiles(ajustes)

    kb = build_knowledge_bases(ajustes, perfiles).for_profile(perfiles.default)

    assert "~" not in str(kb._dir)
