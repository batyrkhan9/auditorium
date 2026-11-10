# The sealed answer key

The score only means something if the agent was built without seeing the
answers or how the mistakes were planted. So that material is encrypted.

## How it works

- Everything under `sealed/` (except its README) is encrypted in git with
  git-crypt. On GitHub and in a normal clone it is unreadable bytes, including
  in history and diffs.
- **Build clone** (a normal `git clone`): no key. `agent`, `connectors`, `ui`
  and `shared` work happens here.
- **Sealed clone** (made with `scripts/sealed-clone <keyfile>`): has the key.
  Only `data/` and `evals/` branches work here. The hooks refuse anything else.
- The key file is `~/.auditorium/sealed.key` on Batyrkhan's machine. He sends it
  to Noriaki privately. It never goes in the repo, an issue or a shared channel.
  Sessions never touch it.

## Rules for data and evals sessions

Nothing learned from `sealed/` leaves it. Not in issues, pull requests, commit
messages, decision files outside `sealed/`, Claude memory, or the public
dataset. Commit messages for sealed changes stay neutral ("data: update
generator"). The only thing that comes out is the aggregate `Scorecard` in
`evals/scorecards/`, which has no transaction ids.

## What it does not protect against

The two of us can read everything, and a session could be told to go and unlock
it. This stops sessions from stumbling on the answers. It does not replace us
keeping agent work in the build clone.

## At submission

Commit the plain files so Ramp can read them: in the sealed clone, remove the
`sealed/**` line from `.gitattributes` and recommit. History then shows the key
was ciphertext the whole time the agent was being built.
