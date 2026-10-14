from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.feed_accepted import FeedAccepted
from ...models.feed_upload import FeedUpload
from ...models.problem import Problem
from ...types import Response


def _get_kwargs(
    *,
    body: FeedUpload,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/v1/feeds",
    }

    _kwargs["files"] = body.to_multipart()

    headers["Content-Type"] = "multipart/form-data; boundary=+++"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> FeedAccepted | Problem | None:
    if response.status_code == 202:
        response_202 = FeedAccepted.from_dict(response.json())

        return response_202

    if response.status_code == 400:
        response_400 = Problem.from_dict(response.json())

        return response_400

    if response.status_code == 401:
        response_401 = Problem.from_dict(response.json())

        return response_401

    if response.status_code == 413:
        response_413 = Problem.from_dict(response.json())

        return response_413

    if response.status_code == 415:
        response_415 = Problem.from_dict(response.json())

        return response_415

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
) -> Response[FeedAccepted | Problem]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient,
    body: FeedUpload,
) -> Response[FeedAccepted | Problem]:
    """Upload a supplier feed (CSV or JSONL)

     The file is streamed to the feeds service without being buffered by the gateway and is
    imported asynchronously; poll `GET /v1/feeds/{id}` for progress. Row-level problems do not
    fail the upload: they are counted in `error_counts` and listed under `/errors`.

    Args:
        body (FeedUpload):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[FeedAccepted | Problem]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient,
    body: FeedUpload,
) -> FeedAccepted | Problem | None:
    """Upload a supplier feed (CSV or JSONL)

     The file is streamed to the feeds service without being buffered by the gateway and is
    imported asynchronously; poll `GET /v1/feeds/{id}` for progress. Row-level problems do not
    fail the upload: they are counted in `error_counts` and listed under `/errors`.

    Args:
        body (FeedUpload):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        FeedAccepted | Problem
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient,
    body: FeedUpload,
) -> Response[FeedAccepted | Problem]:
    """Upload a supplier feed (CSV or JSONL)

     The file is streamed to the feeds service without being buffered by the gateway and is
    imported asynchronously; poll `GET /v1/feeds/{id}` for progress. Row-level problems do not
    fail the upload: they are counted in `error_counts` and listed under `/errors`.

    Args:
        body (FeedUpload):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[FeedAccepted | Problem]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient,
    body: FeedUpload,
) -> FeedAccepted | Problem | None:
    """Upload a supplier feed (CSV or JSONL)

     The file is streamed to the feeds service without being buffered by the gateway and is
    imported asynchronously; poll `GET /v1/feeds/{id}` for progress. Row-level problems do not
    fail the upload: they are counted in `error_counts` and listed under `/errors`.

    Args:
        body (FeedUpload):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        FeedAccepted | Problem
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
