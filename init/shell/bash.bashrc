# /etc/bash.bashrc: interactive bash defaults for every user.
[ -z "$PS1" ] && return
shopt -s checkwinsize histappend globstar 2>/dev/null
HISTSIZE=5000
HISTFILESIZE=10000
HISTCONTROL=ignoredups:erasedups
# user@host:dir$ in Meldra's teal and orange.
PS1='\[\e[38;2;134;220;214m\]\u@\h\[\e[0m\]:\[\e[38;2;248;186;138m\]\w\[\e[0m\]\$ '
alias ls='ls --color=auto'
alias ll='ls -lh --color=auto'
alias la='ls -lAh --color=auto'
alias grep='grep --color=auto'
alias diff='diff --color=auto'
