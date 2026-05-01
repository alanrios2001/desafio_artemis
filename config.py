from dynaconf import Dynaconf
from pathlib import Path

settings = Dynaconf(
    envvar_prefix="DYNACONF",
    settings_files=[
        "dynaconf/settings.toml",
        "dynaconf/db.toml",
        "dynaconf/api.toml",
        "dynaconf/.secrets.toml",
    ],
    root_path=Path(__file__).parent,
    merge_enabled=True,
)

# `envvar_prefix` = export envvars with `export DYNACONF_FOO=bar`.
# `settings_files` = Load these files in the order.
# `root_path` = The root path for the project.
# `merge_enabled` = Merge settings from all sources.
