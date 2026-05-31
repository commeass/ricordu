# Brief — Pipeline de montage photo/vidéo narratif avec sélection IA

## Objectif
Construire une pipeline **Python locale, semi-automatique** qui prend un dossier de photos + clips vidéo d'un événement et produit un **montage narratif** (sortie, voyage, fête…).

Point clé : la pipeline doit **choisir elle-même les meilleurs plans grâce à un modèle d'IA local** (un VLM qui regarde le contenu), puis assembler un montage **hyper qualitatif** : rythme soigné, vrais fondus enchaînés, audio propre, titres lisibles. Pas un slideshow basique.

**100 % local / offline** : Python + FFmpeg + un VLM tournant via **MLX** sur Apple Silicon. Aucun upload, aucun cloud (hors téléchargement initial du modèle).

## Principe semi-auto (en 3 temps)
1. `scan` → liste les médias + métadonnées → `storyboard.json` brut.
2. `ai_select` → **l'IA note et sélectionne** les meilleurs médias, génère les légendes, coche `include`, vise la durée cible → enrichit `storyboard.json` (modifiable).
3. Je relis / j'ajuste le JSON → `render` produit `montage.mp4`.

```bash
python diaporama.py init-model               # télécharge le VLM (1 fois, résumable)
python diaporama.py scan ./input_dir         # → storyboard.json
python diaporama.py ai_select storyboard.json --target 150   # IA note + sélectionne
# (je relis / j'ajuste storyboard.json)
python diaporama.py render storyboard.json    # → montage.mp4
```

## Input / Output
- **Input** : un dossier (photos JPG/PNG/**HEIC**, vidéos MP4/MOV).
- **Output** : `montage.mp4` en **1080p** (H.264 + AAC), audio préservé. 4K en option si les sources le permettent.

## Stack imposée
- **Python 3.12** + **FFmpeg 6.1** appelé directement (pas MoviePy).
- **VLM local via MLX** : `mlx-vlm` (Apple Silicon).
- `Pillow` + `pillow-heif` (HEIC iPhone, `register_heif_opener()`), `opencv-python`, `imagehash`, `scenedetect`.
- Tout offline après le téléchargement du modèle (`HF_HUB_OFFLINE=1` ensuite).

---

## 🧠 Sélection par IA — le cœur du sujet

### Modèle (recommandation vérifiée, mi-2026 — cible : Mac M5 Pro 64 Go)
**VLM via `mlx-vlm`** (format MLX/safetensors uniquement — **PAS de `.gguf`**, qui relève de llama.cpp et n'est pas chargeable par `mlx-vlm`).
- **Défaut** : `mlx-community/Qwen3.6-35B-A3B-4bit` (~20,4 Go, 32 Go+ RAM — OK sur 64 Go). Qwen **3.6** (avril 2026), VLM le plus récent et le plus fort en compréhension de contexte (MMMU 81,7). MoE **A3B = 3B actifs** → rapide malgré ses 35B. Variante qualité équivalente : `...-4bit-DWQ`.
- **Fallback rapide / petite machine** : `mlx-community/Qwen3-VL-4B-Instruct-4bit` (~3,1 Go, 8–16 Go RAM) — débit max, qualité moindre.
- Détecter la RAM au démarrage et choisir le tier (refuser le 35B sous ~32 Go).
- Variantes **Instruct**, pas Thinking (latence inutile pour un score+légende). `temperature=0`, `max_tokens` ~120–200, images redimensionnées à ~1024 px avant inférence.
- **Le gros modèle est viable parce que** le pré-filtre CV + l'échantillonnage par plans font qu'il ne note que quelques **centaines** de frames survivantes, jamais toutes les frames.

### Pipeline de sélection en couches (ordre imposé)
> ⚠️ **Ne JAMAIS faire tourner le VLM sur chaque image brute.** 500 photos + 10 min de vidéo ≈ 18 500 inférences = **7–15 h**. Le pré-filtre CV + le sous-échantillonnage ramènent ça à ~10–20 min (≈ 40× moins d'appels). Le pré-filtre est **obligatoire**.

1. **Pré-filtre CV (sans VLM, multiprocessing CPU)**
   - **Flou** : variance du Laplacien (OpenCV) après downscale à 1024 px (seuil comparable entre appareils).
   - **Doublons** : perceptual hash (`imagehash.phash`) + union-find dans une fenêtre temporelle → on garde 1 représentant par rafale.
   - **Exposition** : histogramme (rejet trop sombre / cramé).
   - **Vidéo** : `PySceneDetect` (`ContentDetector`) découpe chaque clip en **plans (shots)** ; extraction des frames via FFmpeg (`-ss T -frames:v 1`, keyframes / 1 fps), jamais décoder tout le fichier en Python.

2. **Scoring VLM (sur les survivants uniquement, MLX, 1 worker série)**
   - Contrat JSON strict par image : `{"score":0-10,"reason":"<=12 mots","caption":"<=8 mots","tags":[...]}`.
   - Critères : netteté/expo, composition, **contenu humain/émotion (visages, candides > paysages vides)**, pertinence événement, unicité.
   - **Parsing défensif** : regex sur le premier `{...}`, retry 1×, score 0 + `reason:"parse_error"` sinon — **ne jamais crasher**.
   - Calibrage **relatif au dossier** (percentile), pas en valeur absolue.

3. **Vidéo → sous-clips (pas image par image)**
   - Construire un signal de score dans le temps ; sélectionner les spans au-dessus du seuil avec **hystérésis** (anti-flicker).
   - **Snapper les bornes sur les coupures de scène** + points de silence audio (in/out propres), padding, durée min 2–6 s.
   - Signaux additionnels : **énergie audio** (RMS/onset via librosa/ffmpeg → applaudissements, discours, drop musical) et **mouvement** (flux optique / diff inter-frame) — un grand moment qui « bouge » paraît médiocre frame par frame.
   - Chaque span devient son **propre clip** avec `trim_start`/`trim_end` concrets (compatibles render existant).

4. **Ranking vers la durée cible (~2-3 min) — diversité + narration**
   - Sélection gloutonne « knapsack » : tri par score, **quotas par tranche chronologique** (couvrir tout l'événement, pas juste le milieu photogénique), rejet des quasi-doublons déjà admis (pHash/tags) → **évite les 8 plans quasi identiques**.
   - Remplir jusqu'à `target_duration`, puis **re-trier chronologiquement** : le score décide *quoi*, la chronologie décide *l'ordre*.

5. **Write-back semi-auto (idempotent)**
   - Chaque clip reçoit `ai_score`, `ai_caption`, `ai_reason`, `ai_tags`, `ai_include` (décision modèle) + `include` effectif (surchargé par l'humain), `prefilter_reason`, `ai_locked`.
   - **Ne jamais écraser une légende ou un `include` édité à la main** (garde `ai_locked` / drift detection). `--force` re-score tout.
   - Médias rejetés conservés (`include:false`) pour pouvoir les récupérer ; cache sidecar `.ai_cache.json` (clé = chemin + hash) pour des re-runs rapides.
   - Résumé console : `[ai] scoré 142, gardé 38 (~2m29s), 11 flous, 23 doublons, 6 vidéos → 14 sous-clips.`

### Fallback obligatoire
- **Si MLX/modèle indisponible** (Intel, CI, RAM faible, download échoué) → **dégrader en mode CV-only** (netteté + expo + nb/taille de visages + diversité pHash + énergie audio) et logger clairement `running in CV-only mode (VLM unavailable)`. Ne jamais hard-crasher. Le scorer CV est la baseline toujours présente que le VLM augmente.
- **`init-model`** : téléchargement explicite, résumable, barre de progression, hash épinglé ; ensuite tout offline.
- **HEIC décodé une seule fois** (pillow-heif) et réutilisé pour le pré-filtre ET le VLM ; normaliser l'orientation EXIF (portraits non couchés).

---

## Exigences de qualité du montage (FFmpeg)
- **Tri chronologique** : EXIF photo + `creation_time` vidéo, fallback mtime.
- **Vrais fondus enchaînés** entre chaque plan (`xfade` / `acrossfade`), pas des coupes franches.
- **Ken Burns** (zoom/pan doux, sens varié) sur les photos.
- **Audio** : musique de fond optionnelle avec **ducking** (`sidechaincompress`), fade in/out, audio des clips préservé.
- **Titres / cartons** : carton d'intro + légendes (`drawtext`) lisibles (contour/ombre). Les légendes viennent directement de `ai_caption`.
- Normalisation propre (résolution/fps/SAR communs) avant concat.
- **Accélération matérielle Apple** : option `h264_videotoolbox` / `hevc_videotoolbox` pour un rendu rapide sur puce M.

## Durée
- **Cible par défaut : ~2-3 min.** `target_duration` ajuste les durées photo / sélectionne les meilleurs moments si le total dépasse. Durée photo défaut 3–4 s.

## Champs `storyboard.json`
- `clips[]` : `path`, `type`, `include`, `duration`, `caption`, `trim_start`, `trim_end`, `order`, **`ai_score`, `ai_caption`, `ai_reason`, `ai_tags`, `ai_include`, `prefilter_reason`, `ai_locked`**.
- `settings` : `music`, `music_volume`, `ducking`, `title`, `target_duration`, `photo_duration`, `ken_burns`, `transition_duration`, `resolution`, `fps`, `hardware_accel`.
- `settings.ai` : `model`, `blur_var_min`, `phash_hamming_max`, `score_keep_min`, `scene_threshold`, `video_fps_sample`, `subclip_min_s`, `subclip_pad_s`.

## Tests — obligatoire
- Générer des médias factices (photos nettes + photos **volontairement floues/doublons** + ≥1 clip **avec audio** et plusieurs scènes). Faire tourner **scan → ai_select → render**.
- **Vérifier explicitement** :
  - l'IA **exclut** bien les photos floues/doublons et **garde** les nettes (inspecter `ai_score`/`prefilter_reason`) ;
  - la durée finale ≈ `target_duration` ;
  - **l'audio est préservé** (piège : la concat de segments photo muets peut tuer la piste son → donner un silence à chaque segment) ;
  - les **fondus** sont visibles, titre + légendes s'affichent, sortie bien en 1080p ;
  - le **fallback CV-only** fonctionne si on simule l'absence du VLM.
- **Test, puis retest** après correction. Ne pas conclure « ça marche » sans avoir inspecté le fichier de sortie (durée / audio / résolution / sélection).

## Livrables
1. `diaporama.py` (commenté, robuste aux fichiers manquants/corrompus, fallback CV-only).
2. `README.md` : install, usage 3 temps, besoins disque/RAM du modèle, description de chaque champ, limites connues.
3. Un `montage.mp4` d'exemple issu du test.

## Bonus (si le temps le permet)
- Légendes enrichies via GPS EXIF (lieu).
- Pin/exclude de visages ou timestamps « must-include ».
- Manifest inspectable (shortlist classée + vignettes + alternates « just-missed »).
