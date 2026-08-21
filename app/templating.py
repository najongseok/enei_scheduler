from __future__ import annotations
import inspect

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def won(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def hhmm(minutes) -> str:
    if minutes is None:
        return "-"
    h, m = divmod(int(minutes), 60)
    return f"{h}시간 {m}분" if m else f"{h}시간"


templates.env.filters["won"] = won
templates.env.filters["hhmm"] = hhmm


def render(request, name: str, context: dict | None = None, status_code: int = 200):
    """Starlette 버전에 상관없이 동작하는 템플릿 렌더 헬퍼.

    0.29 이전은 TemplateResponse(name, context), 이후는 (request, name, context)
    시그니처를 씁니다. 라우터 코드가 버전에 흔들리지 않도록 여기서 흡수합니다.
    """
    ctx = dict(context or {})
    ctx.setdefault("request", request)
    params = list(inspect.signature(templates.TemplateResponse).parameters)
    if params and params[0] == "request":
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)
    return templates.TemplateResponse(name, ctx, status_code=status_code)
