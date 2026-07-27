"""Pre-built memory tools, namespaced per session (and optionally per scope).

Two shapes, both opt-in at build time:

**Single namespace** (back-compat, most agents):

    tools = memory_tools(namespace=session_id)
    # -> [remember, recall]

**Multi-scope** (for agents with user+household or user+org memory):

    tools = memory_tools(namespaces={
        "personal":  f"user:{email}",
        "household": f"household:{group_id}",
    })
    # -> [remember_personal, recall_personal,
    #     remember_household, recall_household]

**Curation** (``manage=True``, composes with either shape):

    tools = memory_tools(namespace=session_id, manage=True)
    # -> [remember, recall,
    #     list_memories, update_memory, forget_memory]

Off by default. An agent with no curation surface shouldn't carry three
extra tool definitions, and deletion shouldn't appear on an agent that was
never meant to have it.

Each tool closes over its own namespace so storage stays partitioned. The
model picks which tool to call based on prompt rules ("save personal
preferences with remember_personal; save household plans with
remember_household"). When rolling out, update ``rules.md`` to reference
the right tool names for your scopes.
"""

from __future__ import annotations

import contextlib
from typing import Any

from strands import tool

from strands_pg.memory import PgMemoryStore


def memory_tools(
    namespace: str | None = None,
    *,
    namespaces: dict[str, str] | None = None,
    store: PgMemoryStore | None = None,
    top_k: int = 5,
    manage: bool = False,
) -> list[Any]:
    """Build memory tools. Pass ``namespace`` for a single-scope pair, or
    ``namespaces={scope_suffix: storage_namespace, ...}`` for multiple.

    ``manage=True`` adds ``list_memories`` / ``update_memory`` /
    ``forget_memory`` alongside the pair, for agents that curate their own
    memory. Off by default: a chat agent with no curation surface shouldn't
    carry three extra tool definitions in its context, and deletion
    shouldn't appear on an agent never meant to have it.

    Returns a list of Strands ``@tool`` callables ready to merge into an
    ``Agent(tools=[...])`` call.
    """
    if namespace is None and not namespaces:
        raise ValueError(
            "memory_tools requires either namespace=<str> or namespaces={suffix: ns}"
        )
    if namespace is not None and namespaces:
        raise ValueError("memory_tools: pass namespace OR namespaces, not both")

    mem = store or PgMemoryStore()

    if namespace is not None:
        # Single-scope: plain `remember` / `recall`.
        return _build_pair(mem, namespace, suffix="", top_k=top_k, manage=manage)

    # Multi-scope: `remember_<suffix>` / `recall_<suffix>` per entry.
    tools: list[Any] = []
    for suffix, ns in namespaces.items():
        if not suffix or not ns:
            raise ValueError(
                f"memory_tools namespaces entry is invalid: suffix={suffix!r} ns={ns!r}"
            )
        tools.extend(_build_pair(mem, ns, suffix=suffix, top_k=top_k, manage=manage))
    return tools


def _build_pair(
    mem: PgMemoryStore, namespace: str, *, suffix: str, top_k: int, manage: bool = False
) -> list[Any]:
    """Construct remember/recall tools bound to ``namespace``, plus the
    manage trio when ``manage`` is set.

    When ``suffix`` is non-empty, the tool callables are renamed to
    ``remember_<suffix>`` / ``recall_<suffix>`` so the model can tell
    them apart in a multi-scope setup.
    """
    remember_name = f"remember_{suffix}" if suffix else "remember"
    recall_name = f"recall_{suffix}" if suffix else "recall"
    scope_desc = f" ({suffix})" if suffix else ""

    @tool
    def remember_fn(text: str) -> str:
        """Save a durable note.

        Args:
            text: The content to remember.
        """
        mid = mem.add(text, namespace=namespace)
        return f"Saved memory #{mid}"

    @tool
    def recall_fn(query: str, k: int = top_k) -> str:
        """Search durable notes by meaning. Returns top-k hits.

        Args:
            query: Natural-language search query.
            k: Maximum number of hits to return.
        """
        hits = mem.search(query, k=k, namespace=namespace)
        if not hits:
            return "No matches."
        return "\n".join(f"- [{h.id}] {h.text}" for h in hits)

    # Rename the tool callables so Strands emits them under the scoped
    # names. Set __name__ + __qualname__ + the tool_spec name so both the
    # agent's tool registry and the LLM's tool-use payloads see the new
    # identity. Update the docstring to mention the scope.
    remember_fn.__name__ = remember_name
    remember_fn.__qualname__ = remember_name
    remember_fn.__doc__ = (
        f"Save a durable note{scope_desc}.\n\nArgs:\n    text: The content to remember."
    )
    _retag_strands_tool(remember_fn, remember_name)

    recall_fn.__name__ = recall_name
    recall_fn.__qualname__ = recall_name
    recall_fn.__doc__ = (
        f"Search durable notes{scope_desc} by meaning. Returns top-k hits.\n\n"
        "Args:\n    query: Natural-language search query.\n    k: Max hits."
    )
    _retag_strands_tool(recall_fn, recall_name)

    tools = [remember_fn, recall_fn]
    if manage:
        tools.extend(_build_manage(mem, namespace, suffix=suffix))
    return tools


def _build_manage(mem: PgMemoryStore, namespace: str, *, suffix: str) -> list[Any]:
    """Construct list/update/forget tools bound to ``namespace``.

    Every tool passes the closed-over namespace to the store, so the
    cross-tenant hole from #3 can't reappear here: ``PgMemoryStore.delete``
    and ``.update`` both require ``namespace: str``, so a tool that failed
    to pass one would raise rather than reach across tenants.

    Not-found and wrong-tenant deliberately render the same message. The
    store returns ``False`` for both, and distinguishing them would confirm
    that an id exists in somebody else's namespace.
    """
    list_name = f"list_memories_{suffix}" if suffix else "list_memories"
    update_name = f"update_memory_{suffix}" if suffix else "update_memory"
    forget_name = f"forget_memory_{suffix}" if suffix else "forget_memory"
    scope_desc = f" ({suffix})" if suffix else ""

    def _not_found(memory_id: int) -> str:
        return f"No memory #{memory_id} here."

    @tool
    def list_fn(limit: int = 20, offset: int = 0) -> str:
        """List saved notes, newest first.

        Args:
            limit: Maximum number of notes to return.
            offset: How many to skip, for paging through a long list.
        """
        hits = mem.list(namespace=namespace, limit=limit, offset=offset)
        if not hits:
            return "No notes." if offset == 0 else "No more notes."
        lines = []
        for h in hits:
            date = h.created_at.strftime("%Y-%m-%d") if h.created_at else "unknown"
            lines.append(f"- [{h.id}] ({date}) {h.text}")
        return "\n".join(lines)

    @tool
    def update_fn(memory_id: int, text: str) -> str:
        """Reword or correct an existing note, keeping it the same note.

        Use this whenever the note should still exist but say something
        different — fixing wording, dropping a stale detail, merging in a
        correction. Prefer it over deleting and re-saving: that loses the
        note's identity and its original date, and if the re-save fails the
        note is gone. Reserve deletion for notes that shouldn't exist.

        Args:
            memory_id: Id of the note to change, as shown in brackets.
            text: The full replacement text for the note.
        """
        if mem.update(memory_id, text, namespace=namespace):
            return f"Updated memory #{memory_id}"
        return _not_found(memory_id)

    @tool
    def forget_fn(memory_id: int) -> str:
        """Delete a note permanently.

        Only for notes that shouldn't exist at all — wrong, obsolete, or
        saved by mistake. To change what a note *says*, use the update tool
        instead; deleting and re-saving loses its date and identity.

        Args:
            memory_id: Id of the note to delete, as shown in brackets.
        """
        if mem.delete(memory_id, namespace=namespace):
            return f"Deleted memory #{memory_id}"
        return _not_found(memory_id)

    list_fn.__name__ = list_fn.__qualname__ = list_name
    list_fn.__doc__ = (
        f"List saved notes{scope_desc}, newest first.\n\nArgs:\n"
        "    limit: Maximum number of notes to return.\n"
        "    offset: How many to skip, for paging through a long list."
    )
    _retag_strands_tool(list_fn, list_name)

    update_fn.__name__ = update_fn.__qualname__ = update_name
    update_fn.__doc__ = (
        f"Reword or correct an existing note{scope_desc}, keeping it the same "
        "note. Use whenever the note should still exist but say something "
        "different. Prefer over deleting and re-saving, which loses the note's "
        "identity and date.\n\nArgs:\n"
        "    memory_id: Id of the note to change, as shown in brackets.\n"
        "    text: The full replacement text for the note."
    )
    _retag_strands_tool(update_fn, update_name)

    forget_fn.__name__ = forget_fn.__qualname__ = forget_name
    forget_fn.__doc__ = (
        f"Delete a note{scope_desc} permanently. Only for notes that shouldn't "
        "exist at all; to change what a note says, use the update tool.\n\n"
        "Args:\n    memory_id: Id of the note to delete, as shown in brackets."
    )
    _retag_strands_tool(forget_fn, forget_name)

    return [list_fn, update_fn, forget_fn]


def _retag_strands_tool(tool_obj: Any, new_name: str) -> None:
    """Update a Strands tool's advertised name after ``@tool`` has wrapped it.

    Strands' ``@tool`` decorator stores the tool name on the returned
    object (as ``tool_name`` and inside ``tool_spec``). Different SDK
    versions use different attribute names; set whatever exists so the
    renamed tools register correctly across versions.
    """
    for attr in ("tool_name", "_tool_name", "name", "_name"):
        if hasattr(tool_obj, attr):
            with contextlib.suppress(AttributeError, TypeError):
                setattr(tool_obj, attr, new_name)

    spec = getattr(tool_obj, "tool_spec", None) or getattr(tool_obj, "_tool_spec", None)
    if isinstance(spec, dict) and "name" in spec:
        spec["name"] = new_name
