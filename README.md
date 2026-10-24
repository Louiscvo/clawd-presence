# Clawd Presence

Affiche tes sessions Claude Code dans ton activité Discord, avec Clawd (la mascotte de Claude Code) animé selon ce que fait Claude, et un petit résumé de ce sur quoi tu travailles.

![Clawd selon l'état](images/preview.gif)

Sur ton profil Discord, ça donne :

```
Joue à Сlaude Code
Bouton de connexion rouge animé      ← résumé de tes 2 dernières demandes
Écrit du code · 2 sessions           ← ce que fait Claude en ce moment
🎮 1:12:05                           ← depuis le lancement de ta première session
```

| Clawd | Quand |
| --- | --- |
| Casque de chantier | Claude écrit du code, lance des commandes ou dirige des assistants |
| Devant l'ordi | Claude réfléchit, lit le code, cherche sur internet |
| Bras levés | Claude attend ta réponse ou ta permission, ou il a fini |
| Endormi | Session ouverte sans rien faire depuis 15 minutes |

Marche sur **macOS, Windows et Linux**.

## Ce qu'il faut

- **Claude Code** à jour.
- **L'application Discord** ouverte sur le même ordinateur (pas la version navigateur).
- **Python 3.8 ou plus récent** :
  - macOS : souvent déjà là. Sinon : `brew install python`, ou l'installateur de [python.org](https://www.python.org/downloads/).
  - Windows : installateur de [python.org](https://www.python.org/downloads/), en cochant **« Add python.exe to PATH »**. Il faut aussi [Git for Windows](https://git-scm.com/download/win) (Claude Code s'en sert déjà pour lancer les hooks).
  - Linux : déjà installé presque partout.
- Dans Discord : **Paramètres → Confidentialité de l'activité → « Partager mon activité »** activé.

## Installation

Dans Claude Code, tape :

```
/plugin marketplace add Louiscvo/clawd-presence
/plugin install clawd-presence@clawd-presence
```

Puis redémarre Claude Code (ou tape `/reload-plugins`). Ton activité apparaît sur Discord dès ta première demande.

Rien d'autre à faire : pas de programme à lancer au démarrage. Le petit programme qui parle à Discord démarre tout seul avec Claude Code et s'arrête 10 minutes après ta dernière session.

## Réglages

Tape `/config` dans Claude Code, partie **Clawd Presence** :

| Réglage | Par défaut | Effet |
| --- | --- | --- |
| Work summary / Résumé du travail | activé | Affiche le résumé de tes 2 dernières demandes |
| Language / Langue | `auto` | `fr`, `en`, ou `auto` (langue de ton ordinateur) |
| Discord application ID | celui de « Сlaude Code » | Change le nom affiché après « Joue à » (voir plus bas) |

> ⚠️ **Le résumé est visible par tous ceux qui voient ton profil Discord** (amis, membres de tes serveurs). Si tu travailles sur quelque chose de secret, désactive-le. Le résumé est fait par Claude Haiku et consomme un tout petit peu de ton abonnement à chaque nouvelle demande. Le texte complet de tes messages n'est jamais envoyé à Discord, seulement le titre court.

## Comment ça marche

1. Un **hook** Claude Code note l'état de chaque session (ce que fait Claude, tes 2 dernières demandes) dans `~/.claude/plugins/data/clawd-presence…/`.
2. Un **petit programme** (`scripts/presence.py`) lit ces fichiers toutes les 4 secondes et envoie l'activité à l'application Discord, directement sur ton ordinateur (pas de serveur, pas de compte à créer).
3. S'il y a plusieurs sessions, c'est la plus « urgente » qui s'affiche : une qui t'attend passe avant une qui travaille.

## Problèmes fréquents

**Rien ne s'affiche**
- L'application Discord est bien ouverte, et « Partager mon activité » est activé ?
- Tu as redémarré Claude Code après l'installation, et envoyé au moins une demande ?
- Regarde le journal : `~/.claude/plugins/data/clawd-presence-clawd-presence/presence.log`.
- Pour voir ce qui serait envoyé à Discord :
  ```
  python3 ~/.claude/plugins/cache/clawd-presence/clawd-presence/*/scripts/presence.py ~/.claude/plugins/data/clawd-presence-clawd-presence --print
  ```

**Un jeu ou une autre appli passe devant**
Discord choisit lui-même quelle activité montrer en premier. Les applis qui affichent aussi leur propre activité (Modrinth, certains lanceurs…) ont souvent une option pour la couper dans leurs réglages.

**Je veux un autre nom que « Сlaude Code »**
Crée une application sur le [portail développeurs Discord](https://discord.com/developers/applications) avec le nom que tu veux, copie son « Application ID » et colle-le dans le réglage *Discord application ID*. Discord refuse souvent les noms qui contiennent « Claude » : celui par défaut utilise un « С » cyrillique.

## Désinstaller

```
/plugin uninstall clawd-presence@clawd-presence
```

## Crédits

- Clawd est la mascotte de Claude Code, d'Anthropic. Ce projet n'est affilié ni à Anthropic ni à Discord.
- Les animations de Clawd viennent du projet [claude-rpc](https://github.com/rar-file/claude-rpc), détourées et recadrées ici. L'application Discord « Сlaude Code » utilisée par défaut est aussi celle de claude-rpc.
- Code sous licence MIT.

---

## English

Shows your Claude Code sessions as a Discord activity: an animated Clawd that follows what Claude is doing, a short summary of your last two messages, and a timer since your first session started. Works on macOS, Windows and Linux. Needs the Discord desktop app and Python 3.8+ (on Windows, also Git for Windows).

```
/plugin marketplace add Louiscvo/clawd-presence
/plugin install clawd-presence@clawd-presence
```

Restart Claude Code, send a message, and check your Discord profile. Settings (summary on/off, language, Discord application ID) are in `/config`. The summary is public to anyone who can see your profile, so turn it off if your work is private.
