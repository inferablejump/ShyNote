# Install the agent skill

[Documentation](README.md)

Install the [CLI](getting-started.md#install-the-cli-for-use-across-repositories)
first. The skill adds instructions, not the executable or credentials. Run the
copy commands below from the ShyNote source checkout. Copy the whole folder so
its setup reference is included.

## Codex

For your user, across repositories:

```sh
mkdir -p "$HOME/.agents/skills/shynote"
cp -R skills/shynote/. "$HOME/.agents/skills/shynote/"
```

For one repository instead, replace `/path/to/project`:

```sh
mkdir -p /path/to/project/.agents/skills/shynote
cp -R skills/shynote/. /path/to/project/.agents/skills/shynote/
```

Codex discovers user skills in `~/.agents/skills/` and repository skills in
`.agents/skills/`. Invoke this one with `$shynote`, for example: “Use $shynote to
find prior findings about caching.” If it does not appear, restart Codex.
See [the official skill documentation](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills).

## Claude Code

For your user, across repositories:

```sh
mkdir -p "$HOME/.claude/skills/shynote"
cp -R skills/shynote/. "$HOME/.claude/skills/shynote/"
```

For one repository instead, replace `/path/to/project`:

```sh
mkdir -p /path/to/project/.claude/skills/shynote
cp -R skills/shynote/. /path/to/project/.claude/skills/shynote/
```

Claude Code discovers personal skills in `~/.claude/skills/` and project skills
in `.claude/skills/`. Invoke it with `/shynote`. Use `/reload-skills` if a newly
created skills directory is not detected, or start a new session.
See [the Claude Code skill documentation](https://code.claude.com/docs/en/skills#where-skills-live).

## Share and update

Choose user-wide or repository installation for each agent. Commit repository
skill folders if teammates should receive them. These are `.agents/skills/` or
`.claude/skills/`, separate from the default scratch directory `.agent/notes/`.

After updating the ShyNote checkout, repeat the corresponding copy command to
refresh the installed skill. It replaces matching files; preserve any local
customizations first. An editable CLI install does not update copied skills.

The skill follows the notebook's existing configuration. Run `shynote info` in
the target repository to verify that the executable and notebook are available.
