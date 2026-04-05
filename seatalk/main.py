# main.py
import cs_bot
from cs_bot import StartupConfig
from cs_bot.adapters import sop_bot

config = {
    "adapter": {"app_id": "NTAxMjg0OTIwMzg0", "app_secret": "jI9nJrBcHEN7U6XCJtjjvLGi_QFKP560", "signing_secret": "DuRYU54eS2et_U_OKt_OGRzpSCdsrEMO"}
}
cs_bot.init(StartupConfig.model_validate(config))
cs_bot.register_adapter(sop_bot.Adapter)
cs_bot.load_plugin("plugins.echo")

if __name__ == '__main__':
    cs_bot.run(host="0.0.0.0", port=9000)
