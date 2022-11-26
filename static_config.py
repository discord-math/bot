"""
Read a basic ini-like config file at startup. The values inside the config aren't really supposed to change during
execution of the bot. This module implements __getattr__ so that you could write:

    import static_config
    static_config.foo["bar"]
"""

from configparser import ConfigParser, SectionProxy

config_file = "bot.conf"

config= ConfigParser()
config.read(config_file, encoding="utf")

def writeback() -> None:
    """Save the modified config. This will erase comments."""
    with open(config_file, "w", encoding="utf") as f:
        config.write(f)

def __getattr__(name: str) -> SectionProxy:
    try:
        return config[name]
    except KeyError as exc:
        raise AttributeError(*exc.args)
