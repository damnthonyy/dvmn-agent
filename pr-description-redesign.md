# `/describe` — nouvelle approche

Maquette de la sortie cible. Rien de ce document n'est encore implémenté : il sert
à valider le rendu avant de toucher au prompt et au renderer.

## Les trois décisions

**1 — Hiérarchie inversée.** Ce qu'un relecteur ne peut pas déduire du diff passe
en premier. Aujourd'hui « Notes for Reviewer » est en 4ᵉ position, sous un
diagramme décoratif, alors que c'est la seule section à valeur ajoutée nette.

**6 — Sections conditionnées à leur valeur, pas à un compteur.** Le diagramme
n'apparaît que s'il y a une structure à montrer (frontière de module, graphe
d'appel modifié, flux de données), et non parce que `diagram_min_files >= 3`. Une
section sans contenu utile est omise, pas remplie de vide.

**7 — File Walkthrough supprimé.** Il pesait l'essentiel de la description en
tables HTML imbriquées, pour dupliquer l'onglet « Files changed » que GitHub rend
mieux et juste à côté.

---

# Exemple A — PR à fort enjeu

> Rendu tel qu'il apparaîtrait sur la PR. Titre fictif :
> `feat(webhooks): serialise deliveries per repository`

### 🔎 À regarder en premier

<table>
<tr><td>⚠️&nbsp;<strong>Changement de comportement silencieux</strong><br>
<code>retry_after</code> passe de 5&nbsp;s à 30&nbsp;s par défaut. Les appelants qui comptaient sur un retry rapide attendront 6× plus longtemps, sans avertissement.</td></tr>
<tr><td>🧪&nbsp;<strong>Non testé</strong><br>
<code>RateLimiter.reset()</code> et la branche 429 de <code>_handle_response</code> n'ont aucun test. Ce sont les deux chemins que cette PR introduit.</td></tr>
<tr><td>🎯&nbsp;<strong>Hunk porteur</strong><br>
<code>webhook.py:88-121</code> est le seul endroit où le verrou est acquis. Le reste du diff est mécanique — renommages et imports.</td></tr>
<tr><td>🔒&nbsp;<strong>Verrou par dépôt, pas global</strong><br>
Deux dépôts sont toujours traités en parallèle. Si l'intention était de sérialiser globalement, ce n'est pas ce que fait ce code.</td></tr>
</table>

### Type

Enhancement, Bug fix

### Résumé

Les livraisons concurrentes sur un même dépôt écrasaient mutuellement leur état
de traitement : deux pushes rapprochés produisaient un seul commentaire, l'autre
étant perdu en silence. Le traitement est désormais sérialisé par dépôt, derrière
un verrou tenu pour la durée du handler.

Le coût est une latence ajoutée sur les dépôts très actifs : deux livraisons
simultanées sur le même dépôt s'attendent maintenant au lieu de se marcher
dessus.

### Structure

```mermaid
flowchart LR
  hook["Webhook reçu"]
  lock["Verrou par dépôt"]
  queue["File d'attente"]
  handler["handle_request"]
  hook -- "acquiert" --> lock
  lock -- "libre" --> handler
  lock -- "occupé" --> queue
  queue -- "à la libération" --> handler
```

---

# Exemple B — PR mécanique

> Même outil, sur `chore(deps): bump ruff 0.6.1 → 0.7.0`.
> **Trois sections ont disparu**, faute de contenu qui les justifie.

### 🔎 À regarder en premier

<table>
<tr><td>✅&nbsp;<strong>Rien de particulier</strong><br>
Montée de version d'outillage, sans changement de comportement à l'exécution. Les 14 fichiers touchés le sont par le reformatage automatique.</td></tr>
</table>

### Type

Dependencies

### Résumé

Passe `ruff` en 0.7.0 et applique le reformatage qui en découle. La nouvelle
version active `B905` par défaut, d'où les `strict=` ajoutés sur les `zip()`.

*(Pas de section Structure : aucune frontière de module ni graphe d'appel modifié.)*

---

## Ce qui déclenche quoi

| Section | Apparaît quand | Disparaît quand |
|---|---|---|
| **À regarder en premier** | Toujours | Jamais — mais se réduit à une ligne quand il n'y a rien à signaler |
| **Type** | Toujours | Jamais |
| **Résumé** | Toujours | Jamais |
| **Structure** | Une frontière de module, un graphe d'appel ou un flux de données change | Le diff est local à des fonctions existantes, ou purement mécanique |
| ~~File Walkthrough~~ | — | Supprimé |

La règle de la première section est le point sensible : elle doit pouvoir dire
« rien à signaler » de façon crédible. Une carte qui trouve toujours quatre
alertes redevient du bruit en trois PR, et le relecteur cessera de la lire.

## Hors périmètre de cette itération

Écartés pour l'instant, mentionnés pour mémoire : le delta comportemental pour
les appelants, la section « non couvert » dérivée mécaniquement, le classement
des hunks par besoin de scrutin, et le delta entre régénérations.

## Point ouvert

`enforce_template=true` impose aujourd'hui un ordre de sections fixe. L'exemple B
suppose des sections **omissibles**. Les deux exemples ci-dessus gardent un ordre
constant — seule la présence varie —, ce qui reste compatible avec un template
strict à condition d'y autoriser l'omission.
