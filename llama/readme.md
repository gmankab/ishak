### local llama

quadlets that running llama locally in rootless podman


### deploy

```bash
uvx --from ansible-core ansible-playbook --inventory=localhost, --connection=local ansible/playbooks/llama.yml --ask-become-pass
```

### check status

```bash
machinectl shell ishak@.host /bin/systemctl --user status llama-pod.service llamacpp.service mcp-searxng.service searxng.service
```
