# PLAN — Architecture de `diaporama.py`

Fichier unique `diaporama.py`, 3 sous-commandes. Modèle IA = `mlx-community/Qwen3.6-35B-A3B-4bit` (MLX, déjà téléchargé), *thinking* désactivé, `temperature=0`.

```
python diaporama.py scan       <dossier> [-o storyboard.json]
python diaporama.py ai_select  storyboard.json [--target 150] [--order chrono|scene|highlights|narrative]
python diaporama.py render     storyboard.json [-o montage.mp4]
```

---

## 1. `scan <dossier>` — inventaire + chronologie (étapes 1-2 du flow)
- Parcourt le dossier (récursif), filtre par extension : photos `jpg/jpeg/png/heic/heif`, vidéos `mp4/mov/m4v/avi`.
- Date de capture : EXIF `DateTimeOriginal` (photos, via Pillow/pillow-heif), `creation_time` (vidéos, via `ffprobe`), fallback date fichier (mtime).
- Vidéos : `ffprobe` → durée, résolution, présence d'une piste audio.
- GPS : lit lat/lon EXIF si présents (servira au mode « scène/lieu »).
- **Sortie console** : `42 photos, 6 vidéos (3 min 12 s de rush), du 12/05 14:03 au 12/05 19:48`.
- Écrit `storyboard.json` : clips triés chronologiquement + bloc `settings` par défaut. **Aucun appel IA ici** (rapide).

## 2. `ai_select storyboard.json` — description + notation + storyboard (étapes 3-4)
Enrichit `storyboard.json` en place. Pipeline en couches (ordre imposé pour le coût) :

**A. Pré-filtre CV photos (sans IA, multiprocessing)**
- Décodage HEIC→RGB une fois, downscale 1024 px.
- Flou : variance du Laplacien < seuil → `prefilter_reason:"blur"`.
- Expo : histogramme (trop sombre / cramé).
- Doublons : `pHash` + union-find dans une fenêtre temporelle → 1 représentant par rafale, le reste `dup_of:<fichier>`.

**B. Pré-filtre CV vidéos → plans**
- `PySceneDetect` (ContentDetector) découpe chaque clip en **plans**.
- Échantillonnage : keyframe(s) de scène + 1 fps dans les plans longs, extraits via `ffmpeg -ss` (jamais décoder tout le fichier). Même filtre flou/doublon sur les frames.
- ⚠️ On ne note **jamais** toutes les frames — seulement ces points échantillonnés.

**C. Notation VLM (Qwen 3.6, chargé 1 fois, série, `enable_thinking=False`)**
- Par image survivante → JSON strict :
  `{"score":0-10, "sharpness":0-10, "composition":0-10, "faces":0-10, "moment":0-10, "relevance":0-10, "beauty":0-10, "caption":"<=8 mots", "tags":[...]}`.
- Critères = exactement tes axes : qualité, visages, moment, pertinence, beauté.
- Parsing défensif (regex `{...}`, retry 1×, score 0 sinon — jamais de crash). Calibrage en **percentile du dossier**.

**D. Vidéos → sous-clips (avec audio)**
- Courbe de score dans le temps → sélection des spans au-dessus du seuil (hystérésis anti-flicker).
- Bornes **snappées sur les coupures de scène** + creux d'énergie audio (`librosa` RMS → in/out propres), padding, durée 2–6 s.
- Chaque span = un clip à part : `trim_start`/`trim_end`, `ai_score`, `ai_caption`, **audio conservé**.

**E. Sélection vers la durée cible + ordre**
- Budget = `target` (~150 s). Sélection gloutonne diversité-aware (anti quasi-doublons via pHash/tags).
- **Modes d'ordre** (choisi au début) :
  - `chrono` (défaut) : tri par timestamp.
  - `scene` (**ton choix non-chrono**) : voir §3.
  - `highlights` : meilleurs d'abord. `narrative` : courbe calme→pic→fin.

**F. Write-back semi-auto**
- Chaque clip reçoit `ai_score`, sous-scores, `ai_caption`, `ai_tags`, `ai_include`, `prefilter_reason`, `ai_locked`.
- `include` effectif = décision IA, **mais jamais d'écrasement d'une édition manuelle** (caption/include). Médias rejetés gardés (`include:false`) pour récupération. Cache `.ai_cache.json` (re-runs rapides).
- **Console** : `[ai] noté 138, gardé 34 (~2m28s), 9 flous, 21 doublons, 6 vidéos → 12 sous-clips. Édite storyboard.json puis: render`.

## 3. Mode `scene` (regroupement par scène/lieu) — ton choix
- **Chapitrage** des clips en mini-scènes selon : proximité GPS (< ~200 m si dispo) **et/ou** écart temporel (gap > ~20 min = nouveau chapitre) **et/ou** similarité de tags VLM.
- Chaque chapitre = un petit bloc cohérent (ex. « plage », « repas », « soirée »).
- Ordre **des chapitres** : chronologique (par 1er média) par défaut. Ordre **dans** un chapitre : par score décroissant puis temps.
- Carton de chapitre optionnel (titre depuis tag/lieu dominant).

## 4. `render storyboard.json` — montage final (étape 5)
- Photos → segment Ken Burns (`zoompan`, sens varié) **+ piste audio silencieuse** (sinon la concat tue le son).
- Vidéos → `trim` + `scale` au format commun, **audio préservé**.
- Normalisation res/fps/SAR commune, puis **fondus enchaînés** `xfade` (vidéo) + `acrossfade` (audio).
- Carton d'intro + légendes par plan (`drawtext` depuis `ai_caption`, contour/ombre).
- Musique de fond optionnelle : `sidechaincompress` (ducking) + fade in/out.
- Sortie 1080p H.264+AAC. Option `hardware_accel` → `h264_videotoolbox` (rapide sur M5).

---

## 5. Défauts à valider
| Paramètre | Défaut proposé |
|---|---|
| `target_duration` | 150 s (2 min 30) |
| `order` | `chrono` |
| `photo_duration` | 3,5 s |
| `transition_duration` | 0,7 s |
| musique | aucune sauf si fournie |
| `hardware_accel` | activé (videotoolbox) |
| Fallback si MLX absent | mode CV-only (note sur netteté/expo/visages/diversité) |

## 6. Plan de test (factice, auto-généré)
- Génère : 8 photos nettes + 3 floues + 2 doublons (rafale) + 1 clip 3 scènes **avec audio** (tons différents).
- Lance `scan → ai_select → render`. **Vérifie** : floues/doublons exclus, nets gardés, durée ≈ cible, audio préservé, fondus visibles, légendes affichées, 1080p. Puis re-test après corrections. Teste aussi le fallback CV-only.

## 7. Découpage du code
`scan()`, `ai_select()`, `render()` + helpers : `read_media_date`, `extract_gps`, `prefilter_photo`, `split_video_shots`, `sample_frames`, `score_image_vlm`, `subclips_from_scores`, `cluster_scenes`, `rank_to_budget`, `build_segment`, `concat_with_xfade`, `add_music_ducking`. Robuste aux fichiers corrompus, fallback CV-only intégré.
