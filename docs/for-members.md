# The family photo library: how it works for you

<!-- Operator: this page is meant to be handed out or published as is. Replace
"the family account" with the name you gave it, and "whoever runs it" with a
name, before sharing. -->

Our photos live in Immich. Everyone has their own account, and everything you
upload stays yours and private until you decide otherwise. There is also one
extra account, the family account, that owns the photos we share with each
other. Those show up for everyone. A small service keeps that running in the
background; you don't have to do anything with it directly. This page explains
what you'll see and what the few settings do.

## Seeing the family's photos

The family account shares its whole library with you. In Immich that's called
partner sharing. You can always browse it:

- **In the app:** open the Sharing tab (the people icon at the bottom). The
  family account is listed under partners. Tap it to see everything the
  family owns.
- **On the web:** Sharing in the left-hand menu, then the family account under
  Partners.

By default those photos only appear there, not in your main timeline. If you'd
rather have them mixed into your own photos, turn on **Show in timeline** for
the family account:

- **App:** Sharing tab, tap the family account, and flip the switch at the top
  of that screen.
- **Web:** Sharing, click the family account, and use the toggle in the top
  corner.

That's your own setting. Turn it on or off whenever you like; it doesn't
change anything for anyone else. Most of us leave it on, so the family's
photos and our own sit together in one timeline.

## Sharing an album with the family

Make an album as you normally would, then share it with the family account
the way you'd share with any person. That's the whole gesture. Within a few
minutes:

- the album becomes a family album, owned by the family account, with the same
  name and description;
- everyone in the family can see it and add to it;
- the photos in it are moved into the family account. You'll still see them in
  the album, and in your timeline if "Show in timeline" is on. They're just
  owned by the family now.

If someone else had already made a family album with that name, your photos
join it instead of a second album appearing.

Anyone else you had shared that album with keeps their access. Albums you share
privately with one or two family members are left alone; nothing happens to
them unless the family account is on the share list.

## Adding photos to a family album

Any family album, including ones other people made, is open for you to add
your own photos to. Add them as usual and they're moved to the family in the
same way.

There is also an album called the dropbox (ours is named "Family Dropbox").
Photos added to it go into the family's library without ending up in any
album. Use it for photos that belong to the family but don't need an album of
their own, like a nice one of the kids that isn't part of a trip.

## What happens to your copy

When a photo moves, your original goes to your trash, where Immich keeps it
for 30 days before deleting it for good. You'll also see a tag called `family`
on it, which is just a record of what you've handed over. You don't need to do
anything about either. The family's copy is the one everyone sees from then
on.

Everything that matters comes with it: the photo itself, when and where it was
taken, any fixes you made to the date or location in Immich, the description,
the rating, whether it was a favourite, and any cropping or rotating you did.
Two things don't come along: your own tags, because tags are private to each
account anyway, and any likes or comments on it in an album.

## Your phone

The Immich app on your phone backs up your photos by checking which ones the
server already has. Once a moved original leaves your trash, your phone will
think it's missing and upload it again. The service notices and puts it
straight back in the trash, so nothing ends up duplicated, but it's wasted
upload for your phone and wasted work for the server, and it would happen again
every month.

To stop it, turn on one setting in the app: **Settings → Advanced → Sync
remote deletions** (it's marked experimental). With it on, the next time you
open the app after a photo has moved, the app moves your phone's copy to the
phone's own trash, and the loop never starts. Please turn it on. If you're not
sure whether you have, whoever runs the service can tell: they see a count of
re-uploads for each person.

## A few things to know

- A photo that's in one of your public shared links won't be moved while the
  link exists, because moving it would break the link. Take it out of the link
  or delete the link and it moves on the next pass.
- If a photo is part of a stack, moving it takes it out of the stack. The
  stack itself stays.
- Live photos aren't moved yet. They stay in your account for now.
- Taking a photo back from the family isn't possible yet. For now, ask whoever
  runs the service.
- You'll be asked once for an API key from your account, which is what lets
  the service move photos on your behalf. Whoever runs it will tell you which
  boxes to tick when you create it. Nothing else about your account changes.

If something looks wrong, a photo that didn't move or one that moved that
shouldn't have, tell whoever runs the service. Nothing is ever deleted outright;
the original is always in your trash for 30 days.
