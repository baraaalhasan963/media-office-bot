"""Media Office Telegram Bot Entry Point.

This module serves as the primary entry point for starting the bot.
The bot architecture is structured into modular domains:
- controllers/ : coverage, borrow, user_panel, admin, jobs, ai, reports, common, router
- models/      : async database layer (aiosqlite)
- views/       : keyboards, message templates, text & date formatting
- constants.py : bot states, departments, inventory definitions
"""

from controllers.router import main

if __name__ == "__main__":
    main()
