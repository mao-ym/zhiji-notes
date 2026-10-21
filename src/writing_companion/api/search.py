"""搜索 HTTP API 路由。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from writing_companion.services.search import SearchHit, SearchService


router = APIRouter(tags=["search"])
MAX_SEARCH_LIST_LIMIT = 100


class SearchHitResponse(BaseModel):
    """搜索命中响应体。"""

    id: str
    kind: str
    title: str
    snippet: str
    updated_at: str


def get_search_service(request: Request) -> SearchService:
    """从应用容器获取搜索服务。"""

    return request.app.state.container["search_service"]


def to_response(hit: SearchHit) -> SearchHitResponse:
    """将搜索命中转换为 API 响应。"""

    return SearchHitResponse(
        id=hit.id,
        kind=hit.kind,
        title=hit.title,
        snippet=hit.snippet,
        updated_at=hit.updated_at,
    )


@router.get("/api/search", response_model=list[SearchHitResponse])
def search(
    q: Annotated[str, Query()],
    response: Response,
    service: Annotated[SearchService, Depends(get_search_service)],
    kind: Annotated[list[str] | None, Query()] = None,
    start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_LIST_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SearchHitResponse]:
    """搜索文档、对话和记忆。"""

    try:
        page = service.search_page(
            q,
            kinds=kind,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
        response.headers["X-Total-Count"] = str(page.total_count)
        return [
            to_response(hit)
            for hit in page.hits
        ]
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/api/memories/trash/search", response_model=list[SearchHitResponse])
def search_memory_trash(
    q: Annotated[str, Query()],
    response: Response,
    service: Annotated[SearchService, Depends(get_search_service)],
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_LIST_LIMIT)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SearchHitResponse]:
    """只搜索记忆回收站。"""

    try:
        page = service.search_page(
            q,
            kinds=None,
            start=None,
            end=None,
            limit=limit,
            offset=offset,
            include_trash=True,
        )
        response.headers["X-Total-Count"] = str(page.total_count)
        return [
            to_response(hit)
            for hit in page.hits
        ]
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
