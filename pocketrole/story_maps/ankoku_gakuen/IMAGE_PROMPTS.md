# ankoku_gakuen Image Prompts

## Overview

`ankoku_gakuen` のマップ再生演出向けに、画像生成AIへ渡すための汎用プロンプト集。

想定用途:

- Place ごとのシーン背景画像
- 物語全体を俯瞰した「ドラクエのフィールドマップ」風の全体マップ画像

画風の共通方針:

- スーパーファミコン時代レベルのノベルゲーム背景
- 16-bit console era visual novel background feeling
- pixel art 寄りだが、情報量は読みやすく整理
- 日本の私立高校 + 買収後の不穏さ + 音楽青春群像劇
- 明るい青春ではなく、少し影のある夕方や曇天が似合う
- キャラクターは入れない
- UI、文字、吹き出し、ロゴ、ウォーターマークは入れない

## Recommended Sizes

### Final world map background

- 推奨サイズ: `2880 x 1200 px`
- 理由:
  - 横長で Phaser のカメラ追従と相性が良い
  - 複数 Place を横方向に配置しやすい
  - 後で縮小しても情報が潰れにくい

### Place scene backgrounds

- 推奨サイズ: `1280 x 720 px`
- 理由:
  - 16:9 で扱いやすい
  - 背景差し替えや個別演出の素材として流用しやすい

### Safe export notes

- PNG 推奨
- 背景透過は不要
- 文字要素は焼き込まない
- 生成後に必要なら軽くドット感を整える

## Global Prompt Template

以下を各画像プロンプトの末尾に足すと安定しやすい。

```text
Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

## Whole Map Prompt

### 1. World map background

```text
A wide overworld map background for a Japanese private high school drama called Ankoku Gakuen, inspired by a Super Famicom era visual novel mixed with a classic Dragon Quest field map feeling. The world should connect a bought-out private school campus and the nearby town in one cohesive side-scrolling style map. Include a classroom building zone, rooftop access area, music room building, gymnasium, corridor-like school main passage representation, cafeteria area, library wing, school gate, a town park, a small music shop street corner, and a residential home area. The mood is music youth drama with rumors, pressure, performance, sponsorship, hidden tension, and after-school melancholy. Use a readable top-down or slightly angled field-map composition that still feels handcrafted and game-like, with roads and paths clearly linking each place. Nostalgic 16-bit console visual novel atmosphere, slightly dark academic palette, dusk light, emotional but not horror, no characters, no text, no labels, no UI, no speech bubbles, no watermark.

Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

### 2. Whole map negative guidance

```text
Avoid modern 3D rendering, avoid photorealism, avoid chibi characters, avoid giant empty roads, avoid fantasy castles, avoid neon cyberpunk, avoid excessive horror, avoid overly cute pastel tone, avoid text labels, avoid user interface frames.
```

## Place Prompts

### classroom

```text
A Japanese private high school classroom background after a corporate buyout, seen in a Super Famicom era visual novel style. Rows of desks, soft daylight through windows, bulletin boards, a blackboard, subtle signs of competition and gossip in the atmosphere, tidy but emotionally tense. It should feel like ordinary school life mixed with evaluation pressure and rumors slowly spreading. Slightly moody, late morning light, nostalgic 16-bit visual novel background, no characters, no text, no UI, no speech bubbles, no watermark.

Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

### rooftop

```text
A Japanese high school rooftop background with strong wind and open sky, in a Super Famicom era visual novel style. Faded rooftop fence, concrete floor, distant city skyline, dramatic cloud cover, a place for escape, protest, confession, and secret conversation. The scene should feel lonely, dangerous, and emotionally charged, like a fragile refuge above a troubled school. Late afternoon or sunset mood, nostalgic 16-bit visual novel background, no characters, no text, no UI, no speech bubbles, no watermark.

Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

### music_room

```text
A school music room background in a Super Famicom era visual novel style, full of instruments, music stands, amplifier cables, old wooden flooring, and after-school tension. It should feel warm with musical passion but also competitive, as if practice, conflict, and secret night rehearsals could happen here. Include piano, guitars, and rehearsal atmosphere without any people. Slightly dramatic lighting, after-school glow, nostalgic 16-bit visual novel background, no characters, no text, no UI, no speech bubbles, no watermark.

Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

### gym

```text
A Japanese school gymnasium background in a Super Famicom era visual novel style. Large polished floor, basketball hoops, sports markings, echoing emptiness, and the feeling of physical activity mixed with school performance pressure. It should feel broad, slightly lonely, and full of potential motion. Clean but atmospheric, daylight filtering in from high windows, nostalgic 16-bit visual novel background, no characters, no text, no UI, no speech bubbles, no watermark.

Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

### corridor

```text
A Japanese private high school corridor background in a Super Famicom era visual novel style. Long school hallway, classroom doors, reflective floor, windows with soft light, subtle feeling of rumors, passing glances, and emotional near-misses. The corridor should feel like a transit point where tension naturally sparks. Slightly cool lighting, clean perspective, nostalgic 16-bit visual novel background, no characters, no text, no UI, no speech bubbles, no watermark.

Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

### cafeteria

```text
A Japanese school cafeteria background in a Super Famicom era visual novel style. Lunch counters, tables, trays, windows, school posters, and a social atmosphere where factions and rumors become visible. The place should feel lively but a little sharp, as if public evaluation and gossip are always close by. Noon light, colorful but not cheerful, nostalgic 16-bit visual novel background, no characters, no text, no UI, no speech bubbles, no watermark.

Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

### library

```text
A quiet school library background in a Super Famicom era visual novel style. Bookshelves, reading desks, warm lamps, muted sunlight, a space for research, hiding, and collecting evidence. It should feel calm on the surface but carry a faint investigative tension underneath. Elegant, restrained, introspective, nostalgic 16-bit visual novel background, no characters, no text, no UI, no speech bubbles, no watermark.

Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

### school_gate

```text
A Japanese private high school front gate background in a Super Famicom era visual novel style. School gate, entrance path, security post or fence, trees, urban street beyond the campus, and the feeling of arrival, waiting, farewell, and confrontation. It should feel like a threshold between school pressure and the outside world. Morning or evening light, emotionally charged but realistic, nostalgic 16-bit visual novel background, no characters, no text, no UI, no speech bubbles, no watermark.

Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

### town_park

```text
A small Japanese town park background near a private high school, in a Super Famicom era visual novel style. Benches, trees, pathway, soft urban park layout, a little lonely in the evening, a good place for after-school wandering and private conversations. It should feel open but slightly melancholic, suitable for reflection and fragile emotional scenes. Nostalgic 16-bit visual novel background, no characters, no text, no UI, no speech bubbles, no watermark.

Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

### music_shop

```text
A small Japanese music shop background in a Super Famicom era visual novel style. Guitar stands, strings, amplifiers, posters, warm shop light, slightly cramped but exciting interior. It should feel like a place where musicians gather, exchange information, and touch the world beyond school. Cozy but not too cheerful, nostalgic 16-bit visual novel background, no characters, no text, no UI, no speech bubbles, no watermark.

Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

### home

```text
A Japanese teenager's home interior background in a Super Famicom era visual novel style. Private room with bed, desk, smartphone, soft night lighting, personal music-related objects, a feeling of safety mixed with loneliness. It should feel like a place for late-night scrolling, private thoughts, and emotional collapse away from public eyes. Quiet, intimate, nostalgic 16-bit visual novel background, no characters, no text, no UI, no speech bubbles, no watermark.

Style: Super Famicom era visual novel background, 16-bit Japanese game mood, clean readable composition, slightly moody school drama atmosphere, nostalgic pixel-art inspired background, no characters, no text, no UI, no speech bubbles, no watermark.
```

## Optional Master Prompt for Consistency

もし各画像の色味や解像感を揃えたい場合は、各プロンプトの最後にこれを追加:

```text
Use the same visual world across all images: slightly dark navy-and-amber palette, nostalgic Japanese school drama, emotional tension, music-themed youth ensemble story, consistent 16-bit visual novel background style.
```

## Suggested Generation Order

1. 全体マップ背景 `2880 x 1200`
2. `corridor`, `classroom`, `music_room`, `rooftop`
3. `cafeteria`, `library`, `school_gate`
4. `town_park`, `music_shop`, `home`, `gym`

## Final Composition Notes

- 全体マップ画像は「ベース地図」として使う
- Place 個別背景は、その Place の詳細演出や差し替え画面として使う
- もし最終的に 1 枚の全体マップに Place の絵を載せるなら、各 Place 画像はトリミングしやすい構図にする
- 背景中央に大きな主役オブジェクトを置きすぎない
- Place ごとの視認性を優先し、左右または中央に余白を残す
