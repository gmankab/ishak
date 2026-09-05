### deploy

```bash
uvx --from ansible-core ansible-galaxy collection install community.general

uvx --from ansible-core ansible-playbook --inventory=localhost, --connection=local ansible/playbooks/deploy.yml --ask-become-pass
```

### check status

```bash
machinectl shell ishak@.host /bin/systemctl --user status ishak-pod.service llamacpp.service mcp-searxng.service searxng.service
```

### what ansible playbook does

- deny pam because you don't want ishak to use su to run commands as your user
- deny polkit because you don't want ishak to use machinectl or systemd-run to run commands as your user
- disable ssh password auth because you don't want ishak to ssh to your system
- create ishak user without sudo access
- copy containers to ishak homedir
- start systemd services
