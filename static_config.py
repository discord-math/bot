import configparser

config_file = "bot.conf"

config = configparser.ConfigParser()
config.read(config_file, encoding="utf")

def writeback():
    with open(config_file, "w", encoding="utf") as f:
        config.write(f)

def __getattr__(name):
    return config[name]
