"""Telegram bot.

  runner.py     start-up: builds a fresh Dispatcher from the routers below, long polling
  callbacks.py  every button's callback_data, typed (also used by alerts in orders.py / monitor.py)
  common.py     Screen + show(), admin filter, status labels
  queries.py    database reads behind the screens
  customer.py   customer menu: home, balance, orders
  login.py      site login confirmation (/start login_<token>)
  admin.py      admin panel, /bal, /disc, buttons in admin alerts

A screen is a function returning Screen(text, markup); handlers only pick a screen and show() it.
This package imports nothing at package level, so business modules can use `app.bot.callbacks`
without pulling in the handlers (which import them back).
"""
