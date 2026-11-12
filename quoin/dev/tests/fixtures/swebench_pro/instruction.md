A code repository is available in the `/app` directory. Your task is to implement the changes necessary to satisfy the following Pull Request (PR) description:

<pr_description>
## Title Support multiple destination ports in iptables rules

## Description
The Ansible iptables module does not provide a way to specify multiple destination ports in a single rule. Users must create separate rules for each port, which makes configurations more complex and less efficient.
There should be a parameter that accepts a list of destination ports or port ranges and generates an iptables multiport match so multiple ports can be targeted in one rule. This should work for the `tcp`, `udp`, `udplite`, `dccp`, and `sctp` protocols.

## Requirements

- Add a `destination_ports` parameter that accepts a list of ports or port ranges and defaults to an empty list.

- When `destination_ports` is non-empty, use `append_match` and `append_csv` to generate `-m multiport --dports <comma-separated-ports>`.

- Place the multiport arguments after `-j <jump>` and before `-i <in_interface>` in the generated iptables command.

- The `destination_ports` parameter is valid only with the `tcp`, `udp`, `udplite`, `dccp`, and `sctp` protocols.

## New Interfaces

No new interfaces are introduced
</pr_description>

Constraints:
* Do not reference, look up, or copy existing solutions, external PRs, or online workarounds. The implementation must be entirely your own independent work.
