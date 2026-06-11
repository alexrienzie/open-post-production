# micro_structure /: editorial micro-principles guide

*The human-readable companion to `editor/story/_resources/micro_structure/micro_structure_README.md`. The README documents the JSON schema for principle families; this guide is the essay: where the principles come from, what they're actually for, and the cross-family takeaways that don't live in any single file.*

---

## The one big idea

The macro guide argues that ~30 frameworks are mostly the same spine sliced at different resolutions. The micro guide makes the opposite argument: the families below **don't converge**. They come from different rooms, were taught by different people, and frequently disagree about what good editing IS. Snyder's commercial-screenwriting tradition says trim everything to the minimum; Ma/Ozu says hold for a beat longer than feels comfortable. Errol Morris says wait for the unrehearsed moment; broadcast pace says cut at the verbal completion. The traditions are real, they have different lineages, and *all six are right inside their own value system*.

The job of an editor isn't to pick one family and live in it. It's to know all six so the cut can deploy whichever tradition each scene needs. Most documentary scenes need three or four families simultaneously, and the cut that feels mature is the one where every choice is the deliberate output of a tradition the editor knows by name.

---

## Genealogy: where the families come from

Eleven families, six rooms.

**The screenwriting room.** Snyder. Save the Cat (2005) and its predecessors (Field, McKee, Hauge) generated a vocabulary for story-shape failure modes that documentaries inherit: Pope-in-the-Pool, theme-stated-never-preached, lay-pipe, whiff-of-death, debate-is-short. The lineage is *prescriptive*: these are rules a commercial film tries to follow. Documentaries pick them up because the failure modes are real even outside fiction (talking-head exposition really IS the highest-load configuration; debate scenes really DO bloat). One family in this guide, `snyder_principles`, is the harvest from that room.

**The cutting room.** Walter Murch (*In the Blink of an Eye*, 2001) and the broader picture-editor tradition. This is where the actual moves live: cut on emotion not dialogue, L-cuts and J-cuts, trim the entrance keep the exit, match motion across the cut. Murch's lineage runs back through Thelma Schoonmaker, Anne V. Coates, the post-Eisenstein Western montage tradition; it's the practitioners' room, not the writers' room. Two families in this guide, `editing_room_tactical` and `rhythm_pacing`, are from this room. `rhythm_pacing` adds the East Asian counterweight (Ma, Ozu, Tarkovsky) where the Western cutting-room habits of "cut on action" get balanced by "hold past the action."

**The documentary room.** This is where the screenwriting and cutting-room rooms come together and discover that nonfiction breaks both. Maysles brothers, Frederick Wiseman, Errol Morris, Joshua Oppenheimer, the reflexive tradition. Three families in this guide are from this room: `documentary_craft` (evidence, behavior, b-roll as argument), `interview_sync` (how interview material was captured and how it should be cut), and `documentary_honesty` (the ethical constraints: what the editor must NOT do).

**The unscripted-TV room.** Bunim-Murray Productions (*Real World*, 1992), Mark Burnett (*Survivor*, 2000), the modern docuseries tradition (*The Last Dance*, *Free Solo*, *Cheer*), and serial drama (*The Sopranos*, *Breaking Bad*, prestige TV). The most-developed body of editorial wisdom in nonfiction, and the one documentary purists are most likely to dismiss as "manipulative" while quietly using exactly the same moves with less skill. Two families: `unscripted_construction` (reality TV / docuseries craft) and `episodic_pacing` (serial TV pacing). The unscripted room is where you go when you accept that holding the audience's attention through 90+ minutes is a craft problem with technical solutions, not a moral question about the purity of your material.

**The audience-retention room.** Ira Glass and *This American Life* (1995), Jad Abumrad and *Radiolab* (2002), Sarah Koenig and *Serial* (2014), MrBeast and the modern YouTube long-form tradition, the broader podcast and short-form video communities. Unified by a single insight: **the audience can leave at any moment, and the editor's job is to actively prevent that**, by sentence-level momentum in podcast craft, by retention-curve discipline in short-form video. Two families: `voice_driven_nonfiction` (the podcast / radio tradition: action+anecdote alternation, the host turn, the kicker) and `short_form_attention` (the YouTube / TikTok tradition: hook in 5 seconds, pattern interrupts, retention-curve audit, visible payoff). Different mediums, different scales, same fundamental concern. Documentary tradition assumes a committed audience; unscripted-TV assumes a captive broadcast audience; the audience-retention room assumes an audience with a finger on the back button.

**The comedy room.** Christopher Guest (*Waiting for Guffman*, 1996; *Best in Show*, 2000), Errol Morris (the deadpan documentary tradition from *Gates of Heaven* forward), Adam McKay (*The Big Short*, *Don't Look Up*), Larry David and Ricky Gervais (cringe comedy), the broader comedy-writing tradition (rule of three, comic timing, callback structure). One family in this guide: `comic_craft`, which gives the scene-and-cut moves for producing comedy and using tonal contrast as a tension-release valve. The comedy room is structurally separate from the other rooms because comedy operates on a different engine (foolishness vs. virtue, rigidity vs. flexibility, expectation vs. delivery) and its principles often INVERT principles from other rooms (the comic pause builds anticipation, not meaning-settling; the comic button delivers a punch, not a closing thought). Pair with the macro filter `editor/story/_resources/macro_structure/comedy_structure.json` which defines comedy as a film-level mode.

Note the asymmetry: three families from the documentary room, two each from the cutting room / unscripted-TV room / audience-retention room, one each from the screenwriting room and the comedy room. The proportions reflect what documentary editing actually IS (at least when it's done well): 25% nonfiction-specific problems (evidence, interview, ethics), 20% picture-editing craft, 20% audience-holding craft borrowed from unscripted TV, 20% sentence-and-scene-level momentum craft borrowed from podcasts and short-form, 10% imported screenwriting discipline, 5–10% comic craft for any film with tonal absurdity in its material. Editors trained only in the documentary room often discover the other rooms the hardest way: by making films that are right in every individual scene but lose the audience at minute 60, or that have good macro structure but no sentence-by-sentence pull, or that treat naturally-absurd material as if it were tragedy.

---

## The shared vocabulary (how the families intersect)

Some principles recur across families with slight variations. Recognizing the cross-family family-resemblances is half of mastery.

| Concept | Snyder | Murch / Cutting room | Documentary room |
|---|---|---|---|
| Trim aggressively | "Debate is the shortest beat" | "Trim the entrance, keep the exit" | "Don't take a victory lap" |
| Information delivery | "Pope-in-the-Pool" (exposition on motion) | "Audio leads picture into new space" | "B-roll is the argument" / "Show by behavior" |
| Hold past the obvious | (less developed) | "Pace by feeling, not by clock" / "Longest hold = strongest emphasis" | "Verite breath" / "Wait one beat" / Ma's "two-beat rule" |
| Theme delivery | "Theme stated, never preached" | "Cut on emotion not dialogue" | "Specific beats general" / "Show by behavior" |
| Setup planted, harvested later | "Lay pipe" / "Six things that need fixing" | "Pull, don't push" | (less developed; documentaries discover their callbacks in the cut) |
| Ending discipline | "Final image mirrors opening" / "Whiff of death" | "Match motion across cut" | "Don't lie with the cut" |

Where families converge, the move is invisible craft. Where they disagree, the editor has to choose, and the choice should be deliberate, not default.

---

## The families, family by family

### 1. Snyder principles: what the commercial-screenwriting tradition still teaches documentary

The original 10. Most documentary editors don't think of themselves as working in the Snyder tradition; most documentary editors are, anyway, because the failure modes Snyder names are real even outside fiction. Pope-in-the-Pool (exposition rides on motion) is a documentary problem first and a screenplay problem second. Debate-is-short (resist watching characters hesitate) translates one-to-one to documentary (resist watching multiple experts make the same point). The principles transfer; what changes is the vocabulary you'd use if you were caught praising them.

The two principles that DON'T transfer cleanly are *save-the-cat-the-moment* (the audience needs a specific reason to like the protagonist before stakes are imposed) and *whiff-of-death* (the All Is Lost beat needs a specific death: relational, professional, identity). Both require the editor to identify a beat in the rushes that fits a Snyder slot. In documentary, that beat sometimes exists and sometimes doesn't; if it doesn't, the principle can't be forced. Better to know it's missing than to fake it.

### 2. Documentary craft: what makes nonfiction *nonfiction*

The eight principles in `documentary_craft` are the family that names what makes documentary distinctive. The two most load-bearing:

- **The reveal can never live in the interview.** If the only evidence for a key claim is an interviewee asserting it, you don't have the claim; you have a person who says it. The reveal must arrive via document, behavior, contradiction, or the camera catching the moment. Interview is at most corroboration. This principle is what separates investigative journalism that holds up from advocacy that doesn't.

- **B-roll IS the argument.** What the editor cuts TO under a line of interview is the editorial position. There is no neutral b-roll. A line about "years of struggle" cut over footage of someone smiling has one meaning; the same line over the prison release form has another. Editors who think they're being "objective" by not narrating are making arguments via the cut whether they realize it or not. The principle isn't "don't have a position"; it's "know what position you're taking."

Two more principles in the family are about the cut's ethics rather than its mechanics: **the antagonist must be seen** (films often refer to their antagonist without ever putting them on screen, and the audience reads the absence as the editor's weakness) and **the cut IS the argument** (adjacency creates meaning whether the editor intends it or not, so own the adjacencies you create).

### 3. Editing-room tactical: the moves

Murch's lineage. Where the previous family is *what* documentary is, this family is *how* the cut is actually executed at the level of individual seams. Eight principles, all operating at the scale of single cuts and short sequences. The deepest among them:

- **Cut on emotion, not dialogue.** The most common mistake in an interview-driven cut is cutting at the end of every sentence. The audience reads that as mechanical. Cuts that land on emotional transitions (the moment certainty flickers, the beat before the answer arrives) feel inevitable instead of imposed. Most documentary editors know this but don't audit for it; this is the principle most commonly violated in well-meant cuts.

- **Audio leads picture into new space.** Two seconds of street noise under the previous interior tells the audience "we're going outside" before the exterior shot arrives. The audience finds the cut earned, even unconsciously. The hardest principle to teach because it's invisible when it works.

- **Trim the entrance, keep the exit.** The first second of most shots is disposable (camera settling, speaker preparing); the last second is load-bearing (the reaction lands AFTER the spoken word). Editors who don't know this trim equally and produce scenes that feel rushed at the end of every beat.

### 4. Interview & sync: the talking-head problem

Morris's lineage. Documentary is largely an interview-based form, and these five principles diagnose problems specific to that material. The two most universal:

- **Don't ask the question the answer needs.** "Why was Tuesday important?" produces "Because…": the answer is response-shaped, locked to its question. "Tell me what happened Tuesday" produces "On Tuesday I was…"; now the answer is statement-shaped and can be cut into any context. Editors inherit the interviewer's question structure; if the answers are response-shaped, the editing options collapse. This is a field-side principle inherited at edit time; you can only work with what you've got, but knowing the difference tells you which clips are gold.

- **Wait one beat after the natural ending.** The most important sentence often comes after the speaker has "finished." The line they didn't plan to say, the one that's actually true, often arrives 3–10 seconds after the apparent answer. Editors who don't review the post-answer footage miss the strongest material in the rushes.

### 5. Rhythm & pacing: the micro counterpart to Ma

This family is where the East Asian counterweight to the cutting-room lives. The macro filter `ma_negative_space` describes the philosophy abstractly ("meaning lives in the gaps"); these six principles give the actual editor moves:

- **Two-beat rule after heavy emotion.** Give the audience two beats of quiet after an emotional landing before the next thing arrives. Without it, the next scene lands on top of the previous one and the emotion is squandered. With it, the audience finishes feeling what happened before receiving what's next. This is the family's single most actionable principle.

- **Silence is content.** An empty audio track is a positive editorial choice, not a failure. One moment per Act, the cut can go fully silent for 3–6 seconds: the first second after a verdict lands; the breath before a reveal; the morning after the All Is Lost beat. Removing audio lets the audience supply the meaning. Sparingly used, it's the strongest tool in the box; over-used, it loses force.

- **Longest hold = strongest emphasis.** Duration is an editorial claim. In a sequence of cuts, the shot held longest is the one the audience reads as most important, regardless of content. Editors often miss that they're saying this; they hold for technical reasons (no cutaway available) and the audience reads emphasis the editor didn't intend.

### 6. Documentary honesty: the constraints

The five principles in `documentary_honesty` are different from the others. They're not moves; they're rules. They name what the editor must NOT do. Run this checklist on the rough cut before fine-cutting because catching these issues late is much more expensive than catching them at rough-cut stage.

The most important:

- **Don't lie with the cut.** Compositing (placing an answer from Tuesday under a question from Friday, or assembling a "scene" from shots filmed on different days as if continuous) crosses a line. The audience perceives a moment that did not exist. There are gradations, but the principle holds: don't construct moments the camera did not capture. The audience's trust is the film's strongest asset; one compromised scene contaminates the whole.

- **Lower-thirds are evidence.** Naming a subject "former agency official" instead of "former field officer" instead of just their name tells the audience what to weight. The naming choice is an argument disguised as identification. Treat every lower-third as a tiny editorial decision; the audience treats authority labels as evidence, so deploy them deliberately.

- **Ken Burns isn't free.** The slow zoom across a still is a stylistic choice with cost. Used on every photo, the gesture loses force and starts to feel like content padding. Reserve it for 2–4 places per Act where the photo really is a hinge moment.

### 7. Unscripted construction: what reality TV teaches documentary

The family documentary purists dismiss, and the family with the most directly applicable wisdom. Reality TV editors solve problems documentary editors pretend not to have: how to make people watch through commercials, how to construct a coherent character from disparate footage, how to make an interview answer recontextualize a verite scene. Eight principles in `unscripted_construction`; three are load-bearing:

- **The interview button.** Reality TV's structural innovation: every verite scene ends (or is punctuated mid-way) by a testimonial: the same character, in the confessional booth, making the scene's meaning explicit. The verite shows what happened; the testimonial tells you what it meant to the person it happened to. Documentary editors trained only in the verite tradition often leave scenes meaning-less because they refuse to deploy this move out of misplaced purism. Audiences don't register the move; they just feel the scene paid off. This is the single most-borrowable technique in this guide.

- **Characters are constructed in the edit.** Reality TV editors openly acknowledge what documentary editors deny: the character the audience meets is the character the EDITOR chose to construct. Five hours of footage of a person yields a hero, a villain, a comic relief, or a background presence depending on which moments survive into the cut. This isn't deception; it's the unavoidable nature of editing a person down to screen time. The principle is to do it with awareness, not by accident.

- **The reveal escalator.** Reality TV competition shows live and die on this. Each new piece of information should ALSO recontextualize the prior pieces. The audience rereads earlier scenes in their head as new pieces arrive. The cumulative force comes from the rereading, not the new information alone. An investigative thread's stacked reveals should function as an escalator, not independent events.

The frankenbiting principle in this family is the most contested. It names a spectrum from acceptable (trimming a long answer) to unacceptable (constructing a sentence the speaker never meant), and acknowledges that documentary editors operate on the same spectrum without admitting it. The principle isn't permission to frankenbite; it's a vocabulary to discuss what's already happening.

### 8. Episodic pacing: what serial TV teaches features

Originally developed to keep audiences through commercial breaks; refined for the streaming-era binge attention curve. The six principles in `episodic_pacing` generalize beyond episodic work: every Act break in a feature is structurally equivalent to a streaming-era episode break. Three are load-bearing for feature documentary:

- **Cold open hook.** The first 90 seconds of every episode must give the audience a concrete reason to keep watching. For a feature, this applies at the OPENING of each Act, not just at Act I. Act II opening shouldn't recap Act I's resolution; it should pose Act II's question concretely. Act III opening shouldn't summarize Act II's resolution; it should land the question that drives Act III. Audit each Act's first 90 seconds: hook or transition?

- **Act-break tease.** Every Act ends mid-question, not at resolution. The audience leaves wanting an answer the next Act provides. Documentary tradition often closes Acts on resolution (good feeling, sense of closure) and finds the next Act starts from a cold standstill. Hook-endings sustain engagement across Act breaks; resolution-endings don't.

- **Midpoint recommitment.** Long-form serial TV discovered something feature-documentary often misses: audience engagement doesn't sustain automatically across 90+ minutes. There's a midpoint moment, roughly 45–60 minutes into a feature, where engagement needs to be actively renewed, not passively assumed. The Snyder Midpoint names the structural slot; the serial-TV principle names the engagement function. Without active renewal at the Midpoint, the second half feels long even when individual scenes work.

The **tag scene** principle is the family's other distinctive contribution. A short scene AFTER the apparent climax that reframes what just happened. The Sopranos' final-minute moments, Breaking Bad's pre-credits cold scenes. E.g. a scene after the climactic victory that recontextualizes what it means: the subject in the everyday after-world, or a quiet shot showing what the victory cost. This is where Arndt's philosophical climax can land if the climax scene itself can't carry all three.

### 9. Voice-driven nonfiction: what podcast craft teaches documentary

Audio-first storytellers operate without the visual continuity documentary editors take for granted. The discipline that comes from that constraint (building meaning from sentence-by-sentence rhythm, narrator timing, music underscoring) translates back to documentary because video-with-narration faces the same problem at a smaller scale. Seven principles in `voice_driven_nonfiction`; four are load-bearing for documentary:

- **Action + anecdote alternation.** Ira Glass's foundational teaching: a story is built from two alternating modes: ACTION ('then this happened, then this happened') and ANECDOTE ('and that's when I realized'). Action provides momentum; anecdote provides depth. Pure action exhausts; pure anecdote bores. Documentary editors often deliver one or the other across an entire scene; the magic is in the alternation, often at the paragraph or sentence level. This is the most-borrowable single technique in this family.

- **The host turn.** The moment the narrator (or an interview line functioning as narration) steps in to make meaning explicit. The host turn is a precision tool: deploy it when the audience NEEDS the meaning made explicit, refuse it when the material is doing the work. Western documentary tradition often refuses the host turn on principle and produces material the audience can't follow. The discipline is to deploy it deliberately at the 3–5 places in a film where the audience is otherwise working harder than they should.

- **The kicker.** End a scene/segment on a single line that recontextualizes everything just delivered. Different from a `tag_scene` (which is a separate scene); the kicker is the LAST LINE of the existing scene. Most scenes have a kicker buried in the middle of an interview answer; the discipline is to find it and move it to the end. In a document-reveal scene, the kicker isn't the line about WHAT was in the document; it's the line about what it MEANT, placed last.

- **Setup-question-reveal.** Glass's "I had to know what happened next" architecture, applied at the sentence and scene level. Every scene should plant a question the next scene answers. The audience is pulled forward by appetite, not pushed by exposition. Documentary tradition often delivers scenes that resolve their own questions with no carryover; the audience watches because they're committed, not because they NEED to know what's next.

The family's other principles cover music-as-tense-underscore (Radiolab's signature: music as a continuous tension state, with drops as the editorial emphasis), voice-led vs. tape-led scene mode discipline, and the mid-scene pause that lets meaning settle WITHIN scenes (vs. `rhythm_pacing/two_beat_rule` which applies at scene boundaries).

### 10. Short-form attention: what YouTube and TikTok teach long-form

The most data-driven editorial tradition in the catalog. Short-form creators watch retention curves (minute-by-minute graphs of how many viewers are still watching) and recut accordingly. The discipline is empirical: the cut that holds attention is right; the cut that doesn't isn't. Six principles in `short_form_attention`; three transfer most usefully to documentary:

- **Hook in the first 5 seconds, not the first 90.** Short-form discovered that audiences decide to keep watching within FIVE seconds. The hook isn't an event; it's a SIGNAL: a striking visual, a counterintuitive claim, a question the audience can't immediately answer. Long-form documentary doesn't face the same instant-decide pressure, but the principle scales: the first frames of every Act are doing 5-second-hook work whether the editor knows it or not.

- **Pattern interrupt every 8–12 seconds.** Short-form research shows audiences drop without an audio-visual 'event' every 8–12 seconds. An event is anything that changes the pattern: a cut, a zoom, a sound effect, a graphic, a music cue change. The principle doesn't mandate aggressive cutting; it mandates KNOWING which long takes are choices vs. defaults. Compare with `rhythm_pacing/longest_hold_strongest` (which earns sustained holds as emphasis): the two principles reconcile when sustained holds are deliberate and audited.

- **Retention-curve audit.** Short-form creators act on engagement data; documentary tradition treats test-screening data as input to be filtered through artistic intent. Neither extreme is right. The principle in between: when test audiences consistently disengage at a specific moment, that moment IS a problem, even if the editor can articulate why it shouldn't be. The data tells you where to look; what you do is still craft. The most useful principle in this family for a feature documentary in test screenings.

The family's other principles are more genre-specific: **title card placement** (earn the title; don't lead with credits), **visible payoff** (show what they came for, repeatedly), and **the "but wait" signal** (tell the audience a reveal is coming so they're alert when it lands). The "but wait" signal in particular is contested for documentary: it works against surprise, which some documentary moments rely on. But for investigative-thread reveals, a structural signal that "something is about to land" can ensure the audience is alert when the moment arrives.

### 11. Comic craft: what comedy teaches dramatic documentary

The comedy room is structurally separate from the others because comedy operates on a different engine (foolishness vs. virtue, expectation vs. delivery) and its principles often INVERT principles from the dramatic rooms. The comic pause builds anticipation, not meaning-settling. The comic button delivers a punch, not a closing thought. Nine principles in `comic_craft`; four are load-bearing for dramatic documentary with absurd material:

- **Tonal whiplash: hard juxtaposition as tension release.** The signature move of dramatic-comedy filmmaking (Adam McKay's *The Big Short*; Last Week Tonight; *The Death of Stalin*). Cut from gravity to absurdity (or vice versa) WITHOUT transition. The audience's commitment to the previous tonal register makes the new register hit harder; the contrast IS the comedy. Works in both directions: gravity → absurdity releases tension; absurdity → gravity makes the dramatic stakes feel real. For documentary with naturally-absurd material in serious context, this is the central craft move.

- **Earnest absurdity: let the deadpan do the work.** Documentary's secret comedy weapon. The Christopher Guest / Errol Morris tradition: present absurd content WITHOUT winking at it. The subject delivers the absurd line straight; the camera doesn't react; no music cues the joke. The audience does the comedy work themselves, and that's what makes it funny. The moment the cut acknowledges the absurdity (cuing music, cutting to a smirk, inserting a graphic), the comedy collapses because the audience is told they're allowed to laugh. The discipline is to TRUST the audience to find the absurdity without help.

- **Comic relief placement: where to put the comedy beat.** The structural principle behind the user's "every few scenes" instinct. In a primarily dramatic film, comic beats serve as tension release. The audience needs them AFTER heavy emotional work or BEFORE they can absorb more. The rhythm is heavy→light→heavy→light, not heavy→heavy→heavy. Placement rules: after a heavy scene (release pressure); before a heavy scene the audience needs to be receptive to (lower defenses); between two dense informational sequences (memory refresh). NEVER during a heavy scene's emotional climax; comedy mid-climax destroys the gravity that earns the comedy's contrast. Cadence rule of thumb: 8–15 minutes between comic beats.

- **Rule of three: setup, setup, twist.** The most universal micro-comedy structure across every tradition. Three failed attempts escalating into success; three witnesses each more absurd than the last; three reaction shots increasingly bewildered. The first two instances commit the audience to the pattern; the third instance's deviation IS the comedy. Documentary editors often have FIVE similar moments and use them all; the discipline is to cut to three and make the third one carry the comic weight.

Five more principles round out the core family: **comic timing** (the pause that builds anticipation, different from rhythm_pacing's meaning-settling pause), **the comic button** (scene endings on a punch; the comedy version of `unscripted_construction/interview_button` and `voice_driven_nonfiction/the_kicker`), **the callback** (comedy's version of Snyder's lay-pipe: plant a specific detail in Act I, harvest it for comic recognition in Act III), **misdirection / the rug pull** (the audience's commitment to expectation A makes the arrival of B funny), and **the specific absurd detail** (generic things aren't funny; "the tuna melt with extra pickles" is).

**The meta-subversive sub-tradition:** three additional principles cover the Mel Brooks → Monty Python → Deadpool lineage where the FORM itself becomes part of the comedy. **Genre subversion**: deliberately do the opposite of what convention demands; identify the 2-4 specific load-bearing moments where violating the documentary convention (inappropriate music, withheld reaction, unexpected lower-third) surfaces the editorial argument better than the conventional choice. **Pre-emptive acknowledgment**: make the obvious objection before the audience can ("I know what you're thinking…", naming the objection in the film's own voice). The audience's incipient skepticism turns into laughter because the film got there first. **Light meta-wink**: a moment of audience-acknowledgment without breaking the documentary form; the Mel Brooks register, not the Deadpool register. Cards that surface editorial choices, graphics that name the argument, interview lines that comment on form. Budget 2-4 across the runtime; more than that drifts into sketch comedy. These three principles let a film draw on "edgy notes" without abandoning documentary seriousness.

---

## The traditions disagreeing

Here is where the families actually conflict; the editor will hit these moments and has to choose:

**Snyder's "debate is short" vs. Ma's "silence is content."** Snyder says trim every hesitation; Ma says cultivate the pause. Both are right inside their value system. The choice is genre: commercial documentary aiming for broadcast pace will lean Snyder; festival documentary aiming for depth will lean Ma. A single film often wants Ma in one act and Snyder in another. Knowing which one you're applying is the maturity.

**Murch's "cut on emotion" vs. Snyder's "Pope-in-the-Pool."** Murch wants the cut on the emotional beat; Snyder wants the exposition delivered under motion. In an interview-heavy scene, the cut should land on the speaker's emotional shift (Murch) but the picture should be doing exposition work under the talking head (Snyder). The two principles operate on different axes (picture vs. cut timing), so they reconcile most of the time. The conflict is rare but real.

**Documentary craft's "verite breath" vs. Snyder's "debate is short."** Verite breath says let the scene run one beat longer; debate-is-short says trim every redundant moment. These conflict directly. The resolution: verite breath applies to *behavioral* moments (where extending the scene reveals something); debate-is-short applies to *informational* moments (where extending reveals nothing the audience didn't already grasp). Different scenes, different families.

**Interview-sync's "wait one beat" vs. Murch's "cut on emotion."** Interview-sync wants the post-answer beat preserved (the unrehearsed thing the speaker says next); Murch wants the cut on the emotional shift, which is usually mid-sentence. These reconcile in practice: pick the take where the emotional shift IS at the post-answer beat. If the rushes don't offer that, you choose.

**Documentary purist tradition vs. unscripted_construction's "interview button."** The verite tradition says scenes should mean what they observably mean; reality TV says scenes need testimonial buttons that make the meaning explicit. These two traditions roughly cleave on a class line: festival documentary leans verite-purist, broadcast documentary leans reality-TV. The mature position recognizes both: use the interview button when the scene needs editorial closure (most of the time, in long-form work), refuse it when the scene's meaning is best left unstated (the most powerful moments). The wrong move is to refuse the technique on principle, then quietly use weaker substitutes because the scene clearly needs SOMETHING.

**Documentary_honesty's "don't lie with the cut" vs. unscripted_construction's "frankenbite line."** This is the genuine ethical disagreement in the catalog. Documentary purist tradition holds that any composite editing crosses a line; the unscripted tradition acknowledges a spectrum where some composite editing is craft and some is deception. Both are right in different registers. The unscripted tradition is right that the spectrum exists; the documentary tradition is right that the spectrum can be slid down without noticing. The principle is to know which tier you're operating in, and to be able to explain your choice if challenged. The wrong move is to deny the spectrum exists (purist register) OR to use spectrum awareness as license (unscripted register).

**Snyder's "Act III is the finale" vs. episodic_pacing's "tag scene."** Snyder's final-image-mirrors-opening principle wants the cut to end on a rhyme with the Opening Image. The episodic_pacing tag-scene principle wants the cut to end AFTER the climax with a moment that reframes meaning. These reconcile cleanly: the tag scene IS the final image. The principle synthesis: end on a held image that rhymes with the opening AND recontextualizes the climax.

**Voice-driven nonfiction's "host turn" vs. documentary_craft's "show by behavior."** Voice-driven says deploy a narrator/host line to make meaning explicit when the material can't carry it. Documentary purist tradition says show, never tell. These genuinely conflict and the resolution depends on the specific film's voice: observational docs lean show; essay docs lean host turn. The mature position: host turns are precision tools, not default settings. The wrong move is to refuse them on principle, then quietly use weaker substitutes (an interview line awkwardly functioning as narration, a card doing host-turn work) because the scene clearly needs SOMETHING.

**Short-form's "pattern interrupt every 8–12s" vs. rhythm_pacing's "longest hold = strongest emphasis."** Short-form says: no take goes more than 8–12 seconds without an event, or attention drops. Ma/rhythm_pacing says: the longest hold is the editorial emphasis; cultivate it. These are not actually in conflict; they reconcile when sustained holds are DELIBERATE (audited, earned by what came before, used sparingly) rather than DEFAULT (accidental, repeated, applied to material that can't carry them). The principle synthesis: every long take should pass a sustained-hold-is-the-point test. If it does, keep it; if it doesn't, the short-form discipline applies.

**Audience-retention room vs. documentary purist tradition (general).** The deepest disagreement in the catalog. The audience-retention room treats holding attention as a craft problem with technical solutions; the purist tradition treats holding attention as a market consideration that artistic films transcend. This is mostly a class-and-distribution disagreement, not a craft one. Festival documentary distributing to committed audiences leans purist; broadcast/streaming documentary distributing to discoverable audiences leans audience-retention. A film distributing into a hybrid space (festival circuit then streaming) needs both rooms. The position to avoid is dismissing audience-retention principles as "TikTok-brain" while quietly relying on them whenever a scene actually works.

**Comic craft's "earnest absurdity" vs. documentary_craft's "antagonist must be seen."** These genuinely conflict in one specific case: when the natural antagonist of the documentary is themselves a comically-absurd figure. The earnest-absurdity principle says present them deadpan, no commentary; the antagonist-must-be-seen principle says give them institutional weight on screen. When both apply, they pull in different directions. The synthesis: give the antagonist institutional presence (the courthouse, the documents, the procedural language) AND let the absurdity be visible without editorializing. The editor's job is to set up the contrast, not to point at it.

**Rhythm_pacing's "two-beat rule" vs. comic_craft's "comic timing."** Rhythm_pacing's pause is meaning-settling: give the audience two beats to absorb what just happened. Comic timing's pause is anticipation-building: hold so the audience leans forward, then deliver the punch. Both are pauses; both are 0.5–2 seconds; their FUNCTIONS are different. Editors who don't distinguish them end up with comic beats that feel meditative (no laugh) and meditative beats that feel anticipatory (audience expecting a punch that doesn't arrive). The discipline: know which kind of pause each beat needs and audit specifically.

**Comic craft's "tonal whiplash" vs. documentary_honesty's "don't lie with the cut."** Hard juxtaposition between gravity and absurdity is the comic move; constructed contrast can drift into compositing problems if the absurd material is presented as if it occurred in temporal relationship to the dramatic material when it didn't. The synthesis: tonal whiplash works cleanly when both sides of the cut are factually present (just rearranged for effect); it crosses honesty lines when the comic beat is fabricated to relieve dramatic tension. The principle isn't "no comic juxtaposition"; it's "the comic beat has to be REAL material, not imposed."

---

## See also

- `editor/story/_resources/micro_structure/micro_structure_README.md`: JSON schema for principle families + how to add one.
- `editor/story/_resources/macro_structure/macro_structure_guide.md`: the essay for the macro layer (the why behind where you are in the story).
- The macro filter `editor/story/_resources/macro_structure/ma_negative_space.json`: the theoretical underpinning of the `rhythm_pacing` family.
