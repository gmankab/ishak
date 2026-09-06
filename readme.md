### create ishak user

install community.general collection

```bash
uvx --from ansible-core ansible-galaxy collection install community.general
```

create restricted user

```bash
uvx --from ansible-core ansible-playbook --inventory=localhost, --connection=local ansible/playbooks/user-create.yml --ask-become-pass
```

### configured restrictions

list of restrictions configured by create-user.yml ansible playbook:

- deny pam because you don't want ishak to use su to run commands as your user
- deny polkit because you don't want ishak to use machinectl or systemd-run to run commands as your user
- disable ssh password auth because you don't want ishak to ssh to your system
- create ishak user without without wheel group and without sudo access

### deploy local llama

see [llama/readme.md](llama/readme.md)
