"""Self-contained browser page shown after Microsoft sign-in."""


def render_auth_page(success: bool) -> bytes:
    if success:
        eyebrow = "АВТОРИЗАЦИЯ ЗАВЕРШЕНА"
        title = "Вы вошли в аккаунт"
        description = "Всё готово. Можете закрыть эту страницу и вернуться в лаунчер."
        note = "ОКНО БРАУЗЕРА БОЛЬШЕ НЕ НУЖНО"
    else:
        eyebrow = "АВТОРИЗАЦИЯ НЕ ЗАВЕРШЕНА"
        title = "Не удалось войти"
        description = "Вернитесь в лаунчер: там указана причина и что можно сделать дальше."
        note = "ЭТУ СТРАНИЦУ МОЖНО ЗАКРЫТЬ"
    page = f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{title} — ConsoleLauncher</title>
<style>
* {{ box-sizing: border-box; }}
html, body {{ min-height: 100%; margin: 0; }}
body {{
  min-height: 100vh; display: grid; place-items: center; padding: 24px;
  color: #f3eaff; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  background: radial-gradient(ellipse at 50% -10%, #432366 0, transparent 55%),
              radial-gradient(ellipse at 95% 100%, #271039 0, transparent 45%), #0d0813;
}}
main {{ width: min(100%, 560px); }}
.brand {{ margin: 0 0 22px 2px; color: #c4a5df; font-size: 12px; font-weight: 750;
          letter-spacing: .16em; text-transform: uppercase; }}
.card {{ overflow: hidden; border: 1px solid #5f4379; border-radius: 22px;
         background: linear-gradient(150deg, rgba(43, 29, 58, .97), rgba(23, 16, 32, .98));
         box-shadow: 0 28px 90px rgba(0, 0, 0, .38); }}
.accent {{ height: 4px; background: linear-gradient(90deg, #8b5bc2, #d6b9ff, #8b5bc2); }}
.content {{ padding: clamp(28px, 7vw, 54px); }}
.eyebrow {{ margin: 0 0 19px; color: #c6a0f0; font-size: 11px; font-weight: 750;
            letter-spacing: .14em; }}
h1 {{ max-width: 440px; margin: 0; font-size: clamp(30px, 7vw, 44px);
      line-height: 1.12; letter-spacing: -.045em; }}
.description {{ max-width: 390px; margin: 20px 0 0; color: #c8b8d5;
                font-size: 16px; line-height: 1.7; }}
.note {{ margin: 36px 0 0; padding-top: 22px; border-top: 1px solid #483650;
        color: #a98cc2; font-size: 10px; font-weight: 750; letter-spacing: .13em; }}
@media (max-width: 420px) {{ body {{ padding: 18px; }} .card {{ border-radius: 18px; }} }}
</style>
</head>
<body><main>
  <p class="brand">ConsoleLauncher</p>
  <section class="card" aria-labelledby="title">
    <div class="accent"></div>
    <div class="content">
      <p class="eyebrow">{eyebrow}</p>
      <h1 id="title">{title}</h1>
      <p class="description">{description}</p>
      <p class="note">{note}</p>
    </div>
  </section>
</main></body></html>'''
    return page.encode("utf-8")
