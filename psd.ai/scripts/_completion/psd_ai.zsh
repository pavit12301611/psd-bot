#compdef psd_ai psd_ai-backup psd_ai-calendar psd_ai-contacts psd_ai-cookbook psd_ai-docs psd_ai-gallery psd_ai-mail psd_ai-mcp psd_ai-memory psd_ai-notes psd_ai-personal psd_ai-preset psd_ai-research psd_ai-sessions psd_ai-signature psd_ai-skills psd_ai-tasks psd_ai-theme psd_ai-webhook
# Zsh tab-completion for the psd_ai umbrella + sub-CLIs.
#
# Drop in any directory on $fpath, e.g.:
#     fpath=(/path/to/psd_ai-ui/scripts/_completion $fpath)
#     autoload -U compinit; compinit
#
# Then `psd_ai <tab>` completes subcommands; `psd_ai mail <tab>`
# completes mail subcommands; `psd_ai-mail <tab>` works the same.

_psd_ai_scripts_dir() {
    local self="${(%):-%x}"
    while [[ -L "$self" ]]; do self="$(readlink "$self")"; done
    cd "${self:h}/.." && pwd
}

typeset -gA _psd_ai_subs

_psd_ai_refresh() {
    _psd_ai_subs=()
    local dir="$(_psd_ai_scripts_dir)"
    local py="$dir/../venv/bin/python"
    [[ -x "$py" ]] || py="$(command -v python3)"
    local f sub help_out commands
    for f in "$dir"/psd_ai-*; do
        [[ -x "$f" ]] || continue
        case "$f" in
            *.bak|*.pyc|*.pre-*) continue ;;
        esac
        sub="${${f:t}#psd_ai-}"
        help_out=$("$py" "$f" --help 2>/dev/null) || continue
        commands=$(echo "$help_out" | grep -oE '\{[a-z0-9_,-]+\}' | head -1 \
            | tr -d '{}' | tr ',' ' ')
        _psd_ai_subs[$sub]="$commands"
    done
}

_psd_ai() {
    [[ ${#_psd_ai_subs} -eq 0 ]] && _psd_ai_refresh

    local cmd="${words[1]}"

    if [[ "$cmd" == "psd_ai" ]]; then
        if (( CURRENT == 2 )); then
            local -a subs=(${(k)_psd_ai_subs} help)
            _describe 'subcommand' subs
            return
        fi
        local sub="${words[2]}"
        if [[ "$sub" == "help" ]] && (( CURRENT == 3 )); then
            local -a subs=(${(k)_psd_ai_subs})
            _describe 'subcommand' subs
            return
        fi
        if (( CURRENT == 3 )); then
            local -a sc=(${(s/ /)_psd_ai_subs[$sub]})
            _describe 'command' sc
            return
        fi
        return
    fi

    # psd_ai-foo <tab>
    local sub="${cmd#psd_ai-}"
    if (( CURRENT == 2 )); then
        local -a sc=(${(s/ /)_psd_ai_subs[$sub]})
        _describe 'command' sc
        return
    fi
}

_psd_ai "$@"
