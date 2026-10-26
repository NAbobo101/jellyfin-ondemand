# plugins/

Put your own provider modules here (mounted at `/plugins` in the container) and reference them from
`config.toml` with `type = "python"` and `class = "module:Class"`. See
[docs/writing-a-provider.md](../docs/writing-a-provider.md) and `examples/json_api_provider.py`.

Everything in this folder except this README is ignored by git.
