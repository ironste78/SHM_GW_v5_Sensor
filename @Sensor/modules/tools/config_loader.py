# modules/tools/config_loader.py

import os
from configparser import ConfigParser

MODULE_NAME = "SENSOR"

def load_ini(config_path='config.ini', secName=None):
    """
    Carica il file ini, imposta le os.environ (solo se non già presenti),
    e restituisce due dizionari:
      - config_ini: contenuto originale del file
      - config_active: lo stesso, aggiornato con le env effettive
    """
    try:
        config_ini = {}
        config_active = {}

        parser = ConfigParser()
        parser.read(config_path)

        # Mappature sezioni note
        section_mappings = {
            "@sensor": "SENSOR",
            "@manager": "MANAGER",
            "@database": "DATABASE",
            "@tsdb": "TSDB",
            "@discovery": "DISCOVERY",
            "@primaryCloud": "PRIMARY_CLOUD",
            "@secondaryCloud": "SECONDARY_CLOUD"
        }

        for section in parser.sections():
            config_ini[section] = {}
            config_active[section] = {}

            prefix = section_mappings.get(section)

            for key, value in parser.items(section):
                env_key = f"{prefix}_{key.upper()}" if prefix else key.upper()

                # rimuovi commenti inline e spazi
                val = value.split(';', 1)[0].split('#', 1)[0].strip()
                # normalizza booleani (yes/no/true/false -> 1/0)
                low = val.lower()
                if low in ("yes","true","on"):
                    val = "1"
                elif low in ("no","false","off"):
                    val = "0"

                config_ini[section][key] = val

                # Imposta os.environ solo se non già presente
                if env_key not in os.environ:
                    os.environ[env_key] = val

                # In ogni caso, prendi il valore attuale dell'env
                config_active[section][key] = os.getenv(env_key, val)

        #log("[Ini] Configuration loaded successfully", level="info", section=secName)
        print("[Ini] Configuration loaded successfully\n")
        return config_ini, config_active

    except Exception as e:
        #log(f"[Ini] Error while loading the configuration: {e}", level="error", section=secName)
        print(f"[Ini] Error while loading the configuration: {e}\n")
        return {}, {}


def init_environment(config_path='config.ini', show_env=False, log_prefixes=None):
    """
    Inizializza l'ambiente: carica config.ini, imposta env, stampa opzionalmente e restituisce due dizionari.

    :return: (config_ini, config_active)
    """
    config_ini, config_active = load_ini(config_path, MODULE_NAME)

    if show_env:
        print_env_vars(log_prefixes or [
            "TSDB_", "SENSOR_", "MANAGER_", "DATABASE_", "DISCOVERY_",
            "PRIMARY_CLOUD_", "SECONDARY_CLOUD_", "LOG_", "DATA_", "EVENT_"
        ])

    return config_ini, config_active


def compare_configs(config_ini, config_active, section_filter=None):
    from modules.tools.log import log
    """
    Confronta due dizionari di configurazione (ini vs active).
    Mostra override e differenze per debug e tracciabilità.

    :param config_ini: dict, config letto da file ini
    :param config_active: dict, config effettiva usata
    :param section_filter: se fornita, limita il confronto a una lista di sezioni
    """
    log("Differenze tra file ini e ambiente attivo (se presenti):", level="info", section=MODULE_NAME)

    sections = sorted(set(config_ini.keys()) | set(config_active.keys()))
    for section in sections:
        if section_filter and section not in section_filter:
            continue

        ini_items = config_ini.get(section, {})
        active_items = config_active.get(section, {})

        all_keys = sorted(set(ini_items.keys()) | set(active_items.keys()))

        for key in all_keys:
            ini_val = ini_items.get(key)
            active_val = active_items.get(key)

            if ini_val == active_val:
                log(f"  [OK] {section}.{key} = {ini_val}", level="debug", section=MODULE_NAME)
            elif ini_val is None:
                log(f"  [KO] {section}.{key} NON definita in ini → usa env: {active_val}", level="error", section=MODULE_NAME)
            else:
                log(f"  [WARNING]  {section}.{key} = {ini_val} (ini) → {active_val} (env)", level="warning", section=MODULE_NAME)

    log("Fine confronto.", level="info", section=MODULE_NAME)


def print_env_vars(prefixes=None):
    """
    Stampa le variabili d'ambiente filtrando per prefisso. Se prefixes è None, stampa tutto.
    """
    try:
        print("🔍 Variabili d'ambiente attive:\n")
    except UnicodeEncodeError:
        print("[ENV] Variabili d'ambiente attive:\n")

    count = 0
    for key in sorted(os.environ.keys()):
        if prefixes is None or any(key.startswith(p) for p in prefixes):
            print(f"{key} = {os.environ[key]}")
            count += 1

    try:
        print(f"\n✅ Totale variabili stampate: {count}")
    except UnicodeEncodeError:
        print(f"\n[OK] Totale variabili stampate: {count}")


if __name__ == "__main__":
    # Uso stand-alone: stampa solo le variabili rilevanti per il progetto
    prefixes = [
        "TSDB_", "SENSOR_", "MANAGER_", "DATABASE_", "DISCOVERY_",
        "PRIMARY_CLOUD_", "SECONDARY_CLOUD_", "LOG_", "DATA_", "EVENT_", "DATABASE_DIR"
    ]
    print_env_vars(prefixes)
