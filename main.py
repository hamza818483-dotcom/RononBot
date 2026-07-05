"""
main.py — Entrypoint. Runs bot.py as a proper module (not __main__) so that
sheet_handlers.py and mcq_handlers.py can safely do `from bot import ...`
without hitting a circular-import error.
"""
import bot

if __name__ == "__main__":
    bot._run_with_restart()

