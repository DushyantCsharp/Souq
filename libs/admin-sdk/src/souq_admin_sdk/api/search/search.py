from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.filters import Filters
from ...models.lang_param import LangParam
from ...models.problem import Problem
from ...models.search_response import SearchResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    q: str,
    lang: LangParam | Unset = UNSET,
    filters: Filters | Unset = UNSET,
    page_token: str | Unset = UNSET,
    limit: int | Unset = 24,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["q"] = q

    json_lang: str | Unset = UNSET
    if not isinstance(lang, Unset):
        json_lang = lang.value

    params["lang"] = json_lang

    json_filters: dict[str, Any] | Unset = UNSET
    if not isinstance(filters, Unset):
        json_filters = filters.to_dict()
    if not isinstance(json_filters, Unset):
        params.update({f"filters[{key}]": value for key, value in json_filters.items()})

    params["page_token"] = page_token

    params["limit"] = limit

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/v1/search",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Problem | SearchResponse | None:
    if response.status_code == 200:
        response_200 = SearchResponse.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = Problem.from_dict(response.json())

        return response_400

    if response.status_code == 429:
        response_429 = Problem.from_dict(response.json())

        return response_429

    if response.status_code == 500:
        response_500 = Problem.from_dict(response.json())

        return response_500

    if response.status_code == 502:
        response_502 = Problem.from_dict(response.json())

        return response_502

    if response.status_code == 503:
        response_503 = Problem.from_dict(response.json())

        return response_503

    if response.status_code == 504:
        response_504 = Problem.from_dict(response.json())

        return response_504

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Problem | SearchResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    q: str,
    lang: LangParam | Unset = UNSET,
    filters: Filters | Unset = UNSET,
    page_token: str | Unset = UNSET,
    limit: int | Unset = 24,
) -> Response[Problem | SearchResponse]:
    """Search the catalog

    Args:
        q (str):
        lang (LangParam | Unset):
        filters (Filters | Unset): Same shape in requests (`filters[...]`) and responses
            (`applied_filters`, `parsed_query.filters`).
        page_token (str | Unset):
        limit (int | Unset):  Default: 24.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | SearchResponse]
    """

    kwargs = _get_kwargs(
        q=q,
        lang=lang,
        filters=filters,
        page_token=page_token,
        limit=limit,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    q: str,
    lang: LangParam | Unset = UNSET,
    filters: Filters | Unset = UNSET,
    page_token: str | Unset = UNSET,
    limit: int | Unset = 24,
) -> Problem | SearchResponse | None:
    """Search the catalog

    Args:
        q (str):
        lang (LangParam | Unset):
        filters (Filters | Unset): Same shape in requests (`filters[...]`) and responses
            (`applied_filters`, `parsed_query.filters`).
        page_token (str | Unset):
        limit (int | Unset):  Default: 24.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | SearchResponse
    """

    return sync_detailed(
        client=client,
        q=q,
        lang=lang,
        filters=filters,
        page_token=page_token,
        limit=limit,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    q: str,
    lang: LangParam | Unset = UNSET,
    filters: Filters | Unset = UNSET,
    page_token: str | Unset = UNSET,
    limit: int | Unset = 24,
) -> Response[Problem | SearchResponse]:
    """Search the catalog

    Args:
        q (str):
        lang (LangParam | Unset):
        filters (Filters | Unset): Same shape in requests (`filters[...]`) and responses
            (`applied_filters`, `parsed_query.filters`).
        page_token (str | Unset):
        limit (int | Unset):  Default: 24.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Problem | SearchResponse]
    """

    kwargs = _get_kwargs(
        q=q,
        lang=lang,
        filters=filters,
        page_token=page_token,
        limit=limit,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    q: str,
    lang: LangParam | Unset = UNSET,
    filters: Filters | Unset = UNSET,
    page_token: str | Unset = UNSET,
    limit: int | Unset = 24,
) -> Problem | SearchResponse | None:
    """Search the catalog

    Args:
        q (str):
        lang (LangParam | Unset):
        filters (Filters | Unset): Same shape in requests (`filters[...]`) and responses
            (`applied_filters`, `parsed_query.filters`).
        page_token (str | Unset):
        limit (int | Unset):  Default: 24.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Problem | SearchResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            q=q,
            lang=lang,
            filters=filters,
            page_token=page_token,
            limit=limit,
        )
    ).parsed
