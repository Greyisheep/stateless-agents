# Stateless agents, stateful conversations

The demo from **"Horizontally Scaling Agentic Systems with Google ADK on Kubernetes"**
(DevFest Lagos 2026, Claret Ibeawuchi).

A help-desk agent built with Google ADK runs as several pods on Kubernetes. Each act
breaks it the way agent systems break when you scale them out, then fixes it:

| Act | The lesson | What breaks | What fixes it |
|---|---|---|---|
| 1 | Context outlives the pod | A follow-up lands on another pod, and the agent asks for your order number again | Sessions in Postgres, not in the pod |
| 2 | One writer per conversation | Two messages at once, two refunds | A lease per conversation, and a fence on every write |
| 3 | Let a pod finish before it goes | A restart cuts answers mid-sentence | `exec` the server so it hears SIGTERM, and drain |
| 4 | Manage what you can't scale | More conversations than the model's quota: turns refused, and more pods don't help | One first-come line in front of the model |

It needs **no API key and no money**: a fake model speaks Gemini's API and only knows
what is in its prompt, so lost context shows up the same way every time.

## Run it

**One click.** Open this repository in GitHub Codespaces (Code, then Codespaces, then
"Create codespace"). It installs everything and starts the cluster by itself, which takes
a few minutes. Then, in two terminals:

```bash
make board     # terminal 1: the live board
make act1      # terminal 2: then act2, act3, act4
```

**On your own machine.** You need Docker, [kind](https://kind.sigs.k8s.io/), kubectl and
[uv](https://docs.astral.sh/uv/). Then:

```bash
make up        # cluster, images, everything (first run: a few minutes)
make board     # in a second terminal
make act1
```

`make help` lists every step, so you can run an act one command at a time, the way it
was shown on stage. `make reset` puts everything back. `make down` deletes the cluster.

**With real Gemini.** `GEMINI_API_KEY=... make real` points the agent at Gemini instead of
the fake model. Act 1 behaves the same; the others depend on the fake model's timing and
quota knob.

## What's inside

```text
agent/        the help-desk agent: FastAPI + ADK Runner, two tools (lookup_order,
              issue_refund), the session switch, the lease and fence, the model gate
fakemodel/    a stand-in for the Gemini API: no key, a quota knob, answers only from
              what is in the prompt
k8s/          kind config and the manifests: agent, Postgres, Redis, the fake model
demo/         drive.py sends each act's turns; board.py shows them live
```

The switches (where sessions live, the lease, the gate) are flags in Redis that every pod
reads on every turn, so the demo flips them with no restart. In a real system they are
not switches: you would always keep sessions in a database, always take the lease, and
always put calls to a shared model behind one line.

Act 3 is the only one that restarts pods, because restarting is the point. `make
drain-test` starts six customers asking long questions and restarts every pod 8 seconds
in. With the shell entrypoint, the old pods ignore SIGTERM, keep taking turns and are
killed 30 seconds later, mid-answer. With `exec`, none are cut.

## Two bugs this demo found in its own gate

Pods die, so anything a pod holds in a shared store has to expire without it.

- **A dead pod's slot.** A pod killed while holding a model slot never gives it back.
  A held slot now expires after 20 seconds, longer than any model call.
- **A dead pod's place in line.** A pod killed while waiting stays at the front of the
  line, and with a first-come line, everyone waits behind it forever. Every waiter now
  refreshes a one-second heartbeat, and the gate drops a head of the line whose heartbeat
  has stopped.

Both showed up in Act 3: the pods killed in its first half blocked every turn in its
second half until these fixes went in.

## The ideas, in one place

- **Context outlives the pod.** Any pod can take the next message, because nothing it
  needs lives only in that pod.
- **One owner at a time.** A lease says which pod owns a conversation right now. A fence
  (a number that only goes up, checked by the database on every write) refuses the late
  write of a pod that lost its lease.
- **One line for the model.** Kubernetes scales pods, not your model quota. Every model
  call takes a slot from one budget shared by every pod, in the order calls arrived.

Run agents like web servers. Run context like a database.
