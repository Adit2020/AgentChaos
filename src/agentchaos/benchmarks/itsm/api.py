"""HTTP interface for one isolated ITSM environment."""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from .database import (
    AssignmentEvent,
    Environment,
    OperationRejected,
    Rejection,
    Ticket,
    assign_ticket,
    get_history,
    get_ticket,
    require_assignment_actor,
)
from .service_fault import (
    TOOL_CALL_HEADER,
    AssignmentIntent,
    AssignmentMonitor,
    RejectedAssignment,
    unavailable_response,
)

_STATUS = {
    Rejection.UNKNOWN_TICKET: 404,
    Rejection.FORBIDDEN: 403,
    Rejection.INVALID_ASSIGNEE: 422,
    Rejection.STALE_VERSION: 409,
}


class AssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee_id: str = Field(min_length=1)
    expected_version: int = Field(strict=True, ge=1)


@dataclass(frozen=True)
class Assignment:
    ticket_id: str
    assignee_id: str | None
    version: int


def create_app(
    environment: Environment, *, monitor: AssignmentMonitor | None = None
) -> FastAPI:
    app = FastAPI(title="AgentChaos ITSM")
    bearer = HTTPBearer(auto_error=False)
    users_by_token = {token: user_id for user_id, token in environment.tokens.items()}

    def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        actor_id = users_by_token.get(credentials.credentials) if credentials else None
        if actor_id is None:
            raise HTTPException(
                status_code=401,
                detail="Missing or invalid fixture token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return actor_id

    @app.exception_handler(OperationRejected)
    async def rejected(request: Request, exc: OperationRejected) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS[exc.reason], content={"detail": exc.reason.value}
        )

    @app.get("/tickets/{ticket_id}", dependencies=[Depends(authenticate)])
    def ticket_detail(ticket_id: str) -> Ticket:
        return get_ticket(environment.database, ticket_id)

    @app.get("/tickets/{ticket_id}/assignment", dependencies=[Depends(authenticate)])
    def current_assignment(ticket_id: str) -> Assignment:
        ticket = get_ticket(environment.database, ticket_id)
        return Assignment(ticket.id, ticket.assignee_id, ticket.version)

    @app.put("/tickets/{ticket_id}/assignment", response_model=Assignment)
    def update_assignment(
        ticket_id: str,
        payload: AssignmentRequest,
        actor: Annotated[str, Depends(authenticate)],
        request: Request,
    ) -> Assignment | Response:
        observed_request = None
        if monitor is not None and ticket_id == monitor.ticket_id:
            require_assignment_actor(environment.database, actor)
            observed_request = monitor.receive(
                AssignmentIntent(
                    actor_id=actor,
                    assignee_id=payload.assignee_id,
                    expected_version=payload.expected_version,
                ),
                tool_call_id=request.headers.get(TOOL_CALL_HEADER) or None,
            )
            if isinstance(observed_request, RejectedAssignment):
                return Response(
                    content=unavailable_response(observed_request.request_id),
                    media_type="application/json",
                    status_code=503,
                    background=BackgroundTask(monitor.mark_response_sent),
                )

        ticket = assign_ticket(
            environment.database,
            ticket_id,
            actor,
            payload.assignee_id,
            payload.expected_version,
        )
        if monitor is not None and observed_request is not None:
            monitor.record_commit(observed_request, ticket.version)
        return Assignment(ticket.id, ticket.assignee_id, ticket.version)

    @app.get("/tickets/{ticket_id}/audit", dependencies=[Depends(authenticate)])
    def audit_history(ticket_id: str) -> list[AssignmentEvent]:
        return get_history(environment.database, ticket_id)

    return app
