import asyncio
import typing
from datetime import timedelta

import context
from core import parse_typename, ValueSpec, SdkAddress
from messages import Message, EgressMessage
from statefun_builder import StatefulFunctions, StatefulFunction

# generated function protocol
from request_reply_pb2 import ToFunction, FromFunction, Address, TypedValue
from storage import resolve, Cell

from dataclasses import dataclass


@dataclass
class DelayedMessage:
    is_cancellation: bool = None
    duration: int = None
    message: Message = None,
    cancellation_token: str = None


class UserFacingContext(context.Context):
    __slots__ = (
        "_self_address", "_outgoing_messages", "_outgoing_delayed_messages", "_outgoing_egress_messages", "_storage",
        "_caller")

    def __init__(self, address, storage):
        self._self_address = address
        self._outgoing_messages = []
        self._outgoing_delayed_messages: typing.List[DelayedMessage] = []
        self._outgoing_egress_messages = []
        self._storage = storage
        self._caller = None

    @property
    def address(self) -> SdkAddress:
        """

        :return: the address of the currently executing function. the address is of the form (typename, id)
        """
        return self._self_address

    @property
    def storage(self):
        return self._storage

    @property
    def caller(self):
        return self._caller

    def send(self, message: Message):
        """
        Send a message to a function.

        :param message: a message to send.
        """
        self._outgoing_messages.append(message)

    def send_after(self, duration: timedelta, message: Message, cancellation_token: str = ""):
        """
        Send a message to a target function after a specified delay.

        :param duration: the amount of time to wait before sending this message out.
        :param message: the message to send.
        :param cancellation_token: an optional cancellation token to associate with this message.
        """
        ms = int(duration.total_seconds() * 1000.0)
        record = DelayedMessage(is_cancellation=False, duration=ms, message=message,
                                cancellation_token=cancellation_token)
        self._outgoing_delayed_messages.append(record)

    def cancel_delayed_message(self, cancellation_token: str):
        """
        Cancel a delayed message (message that was sent using send_after) with a given token.

        Please note that this is a best-effort operation, since the message might have been already delivered.
        If the message was delivered, this is a no-op operation.
        """
        record = DelayedMessage(is_cancellation=True, cancellation_token=cancellation_token)
        self._outgoing_delayed_messages.append(record)

    def send_egress(self, message: EgressMessage):
        """
        Send a message to an egress.

        :param message: the EgressMessage to send.
        """
        self._outgoing_egress_messages.append(message)


# -------------------------------------------------------------------------------------------------------------------
# Protobuf Helpers
# -------------------------------------------------------------------------------------------------------------------

def sdk_address_from_pb(addr: Address) -> typing.Optional[SdkAddress]:
    if not addr or (not addr.namespace and not addr.type and not addr.id):
        return None
    return SdkAddress(namespace=addr.namespace,
                      name=addr.type,
                      id=addr.id,
                      typename=f"{addr.namespace}/{addr.type}")


# noinspection PyProtectedMember
def collect_success(ctx: UserFacingContext) -> FromFunction:
    pb_from_function = FromFunction()
    pb_invocation_result = pb_from_function.invocation_result
    collect_messages(ctx._outgoing_messages, pb_invocation_result)
    collect_delayed(ctx._outgoing_delayed_messages, pb_invocation_result)
    collect_egress(ctx._outgoing_egress_messages, pb_invocation_result)
    collect_mutations(ctx._storage._cells, pb_invocation_result)
    return pb_from_function


def collect_failure(missing_state_specs: typing.List[ValueSpec]) -> FromFunction:
    pb_from_function = FromFunction()
    incomplete_context = pb_from_function.incomplete_invocation_context
    missing_values = incomplete_context.missing_values
    for state_spec in missing_state_specs:
        missing_value = missing_values.add()
        missing_value.state_name = state_spec.name
        missing_value.type_typename = state_spec.type.typename

        protocol_expiration_spec = FromFunction.ExpirationSpec()
        if not state_spec.after_write and not state_spec.after_call:
            protocol_expiration_spec.mode = FromFunction.ExpirationSpec.ExpireMode.NONE
        else:
            protocol_expiration_spec.expire_after_millis = state_spec.duration
            if state_spec.after_call:
                protocol_expiration_spec.mode = FromFunction.ExpirationSpec.ExpireMode.AFTER_INVOKE
            elif state_spec.after_write:
                protocol_expiration_spec.mode = FromFunction.ExpirationSpec.ExpireMode.AFTER_WRITE
            else:
                raise ValueError("Unexpected state expiration mode.")
        missing_value.expiration_spec.CopyFrom(protocol_expiration_spec)
    return pb_from_function


def collect_messages(messages: typing.List[Message], pb_invocation_result):
    pb_outgoing_messages = pb_invocation_result.outgoing_messages
    for message in messages:
        outgoing = pb_outgoing_messages.add()

        namespace, type = parse_typename(message.target_typename)
        outgoing.target.namespace = namespace
        outgoing.target.type = type
        outgoing.target.id = message.target_id
        outgoing.argument.CopyFrom(message.typed_value)


def collect_delayed(delayed_messages: typing.List[DelayedMessage], invocation_result):
    delayed_invocations = invocation_result.delayed_invocations
    for delayed_message in delayed_messages:
        outgoing = delayed_invocations.add()

        if delayed_message.is_cancellation:
            # handle cancellation
            outgoing.cancellation_token = delayed_message.cancellation_token
            outgoing.is_cancellation_request = True
        else:
            message = delayed_message.message
            namespace, type = parse_typename(message.target_typename)

            outgoing.target.namespace = namespace
            outgoing.target.type = type
            outgoing.target.id = message.target_id
            outgoing.delay_in_ms = delayed_message.duration
            outgoing.argument.CopyFrom(message.typed_value)
            if delayed_message.cancellation_token is not None:
                outgoing.cancellation_token = delayed_message.cancellation_token


def collect_cancellations(tokens: typing.List[str], invocation_result):
    outgoing_cancellations = invocation_result.outgoing_delay_cancellations
    for token in tokens:
        if token:
            delay_cancelltion = outgoing_cancellations.add()
            delay_cancelltion.cancellation_token = token


def collect_egress(egresses: typing.List[EgressMessage], invocation_result):
    outgoing_egresses = invocation_result.outgoing_egresses
    for message in egresses:
        outgoing = outgoing_egresses.add()

        namespace, type = parse_typename(message.typename)
        outgoing.egress_namespace = namespace
        outgoing.egress_type = type
        outgoing.argument.CopyFrom(message.typed_value)


def collect_mutations(cells: typing.Dict[str, Cell], invocation_result):
    for key, cell in cells.items():
        if not cell.dirty:
            continue
        mutation = invocation_result.state_mutations.add()
        mutation.state_name = key
        val: typing.Optional[TypedValue] = cell.typed_value
        if val is None:
            # it is deleted.
            mutation.mutation_type = FromFunction.PersistedValueMutation.MutationType.Value('DELETE')
        else:
            mutation.mutation_type = FromFunction.PersistedValueMutation.MutationType.Value('MODIFY')
            mutation.state_value.CopyFrom(val)


# --------------------------------------------------------------------------------------------------------------------
# The main Request Reply Handler.
# --------------------------------------------------------------------------------------------------------------------

class RequestReplyHandler(object):
    def __init__(self, functions: StatefulFunctions):
        if not functions:
            raise ValueError("functions must be provided.")
        self.functions = functions

    def handle_sync(self, request_bytes: typing.Union[str, bytes, bytearray]) -> bytes:
        return asyncio.run(self.handle_async(request_bytes))

    async def handle_async(self, request_bytes: typing.Union[str, bytes, bytearray]) -> bytes:
        # parse
        pb_to_function = ToFunction()
        pb_to_function.ParseFromString(request_bytes)
        # target address
        pb_target_address = pb_to_function.invocation.target
        sdk_address = sdk_address_from_pb(pb_target_address)
        # target stateful function
        target_fn: StatefulFunction = self.functions.for_typename(sdk_address.typename)
        if not target_fn:
            raise ValueError(f"Unable to find a function of type {sdk_address.typename}")
        # resolve state
        res = resolve(target_fn.storage_spec, sdk_address.typename, pb_to_function.invocation.state)
        if res.missing_specs:
            pb_from_function = collect_failure(res.missing_specs)
            return pb_from_function.SerializeToString()
        # invoke the batch
        ctx = UserFacingContext(sdk_address, res.storage)
        fun = target_fn.fun
        pb_batch = pb_to_function.invocation.invocations
        if target_fn.is_async:
            for pb_invocation in pb_batch:
                msg = Message(target_typename=sdk_address.typename, target_id=sdk_address.id,
                              typed_value=pb_invocation.argument)
                ctx._caller = sdk_address_from_pb(pb_invocation.caller)
                # await for an async function to complete.
                # noinspection PyUnresolvedReferences
                await fun(ctx, msg)
        else:
            for pb_invocation in pb_batch:
                msg = Message(target_typename=sdk_address.typename, target_id=sdk_address.id,
                              typed_value=pb_invocation.argument)
                ctx._caller = sdk_address_from_pb(pb_invocation.caller)
                # we need to call the function directly ¯\_(ツ)_/¯
                fun(ctx, msg)
        # collect the results
        pb_from_function = collect_success(ctx)
        return pb_from_function.SerializeToString()
