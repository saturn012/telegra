# -*- coding: future_fstrings -*-
# mautrix-telegram - A Matrix-Telegram puppeting bridge
# Copyright (C) 2018 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Optional, Callable
import asyncio

from telethon.errors import *
from telethon.tl.types import ChatForbidden, ChannelForbidden
from mautrix_appservice import MatrixRequestError, IntentAPI

from .. import portal as po, user as u
from . import (command_handler, CommandEvent,
               SECTION_ADMIN, SECTION_CREATING_PORTALS, SECTION_PORTAL_MANAGEMENT)


@command_handler(needs_admin=True, needs_auth=False, name="set-pl",
                 help_section=SECTION_ADMIN,
                 help_args="<_level_> [_mxid_]",
                 help_text="Set a temporary power level without affecting Telegram.")
async def set_power_level(evt: CommandEvent) -> None:
    try:
        level = int(evt.args[0])
    except KeyError:
        return await evt.reply("**Usage:** `$cmdprefix+sp set-power <level> [mxid]`")
    except ValueError:
        return await evt.reply("The level must be an integer.")
    levels = await evt.az.intent.get_power_levels(evt.room_id)
    mxid = evt.args[1] if len(evt.args) > 1 else evt.sender.mxid
    levels["users"][mxid] = level
    try:
        await evt.az.intent.set_power_levels(evt.room_id, levels)
    except MatrixRequestError:
        evt.log.exception("Failed to set power level.")
        return await evt.reply("Failed to set power level.")


@command_handler(help_section=SECTION_PORTAL_MANAGEMENT,
                 help_text="Get a Telegram invite link to the current chat.")
async def invite_link(evt: CommandEvent) -> None:
    portal = po.Portal.get_by_mxid(evt.room_id)
    if not portal:
        return await evt.reply("This is not a portal room.")

    if portal.peer_type == "user":
        return await evt.reply("You can't invite users to private chats.")

    try:
        link = await portal.get_invite_link(evt.sender)
        return await evt.reply(f"Invite link to {portal.title}: {link}")
    except ValueError as e:
        return await evt.reply(e.args[0])
    except ChatAdminRequiredError:
        return await evt.reply("You don't have the permission to create an invite link.")


async def user_has_power_level(room: str, intent, sender: u.User, event: str, default: int = 50
                               ) -> None:
    if sender.is_admin:
        return True
    # Make sure the state store contains the power levels.
    try:
        await intent.get_power_levels(room)
    except MatrixRequestError:
        return False
    return intent.state_store.has_power_level(room, sender.mxid,
                                              event=f"net.maunium.telegram.{event}",
                                              default=default)


async def _get_portal_and_check_permission(evt: CommandEvent, permission: str,
                                           action: Optional[str] = None) -> None:
    room_id = evt.args[0] if len(evt.args) > 0 else evt.room_id

    portal = po.Portal.get_by_mxid(room_id)
    if not portal:
        that_this = "This" if room_id == evt.room_id else "That"
        return await evt.reply(f"{that_this} is not a portal room."), False

    if not await user_has_power_level(portal.mxid, evt.az.intent, evt.sender, permission):
        action = action or f"{permission.replace('_', ' ')}s"
        return await evt.reply(f"You do not have the permissions to {action} that portal."), False
    return portal, True


def _get_portal_murder_function(action: str, room_id: str, function: Callable, command: str,
                                completed_message: str) -> None:
    async def post_confirm(confirm) -> None:
        confirm.sender.command_status = None
        if len(confirm.args) > 0 and confirm.args[0] == f"confirm-{command}":
            await function()
            if confirm.room_id != room_id:
                return await confirm.reply(completed_message)
        else:
            return await confirm.reply(f"{action} cancelled.")

    return {
        "next": post_confirm,
        "action": action,
    }


@command_handler(needs_auth=False, needs_puppeting=False,
                 help_section=SECTION_PORTAL_MANAGEMENT,
                 help_text="Remove all users from the current portal room and forget the portal. "
                           "Only works for group chats; to delete a private chat portal, simply "
                           "leave the room.")
async def delete_portal(evt: CommandEvent) -> None:
    portal, ok = await _get_portal_and_check_permission(evt, "unbridge")
    if not ok:
        return

    evt.sender.command_status = _get_portal_murder_function("Portal deletion", portal.mxid,
                                                            portal.cleanup_and_delete, "delete",
                                                            "Portal successfully deleted.")
    return await evt.reply("Please confirm deletion of portal "
                           f"[{portal.alias or portal.mxid}](https://matrix.to/#/{portal.mxid}) "
                           f"to Telegram chat \"{portal.title}\" "
                           "by typing `$cmdprefix+sp confirm-delete`"
                           "\n\n"
                           "**WARNING:** If the bridge bot has the power level to do so, **this "
                           "will kick ALL users** in the room. If you just want to remove the "
                           "bridge, use `$cmdprefix+sp unbridge` instead.")


@command_handler(needs_auth=False, needs_puppeting=False,
                 help_section=SECTION_PORTAL_MANAGEMENT,
                 help_text="Remove puppets from the current portal room and forget the portal.")
async def unbridge(evt: CommandEvent) -> None:
    portal, ok = await _get_portal_and_check_permission(evt, "unbridge")
    if not ok:
        return

    evt.sender.command_status = _get_portal_murder_function("Room unbridging", portal.mxid,
                                                            portal.unbridge, "unbridge",
                                                            "Room successfully unbridged.")
    return await evt.reply(f"Please confirm unbridging chat \"{portal.title}\" from room "
                           f"[{portal.alias or portal.mxid}](https://matrix.to/#/{portal.mxid}) "
                           "by typing `$cmdprefix+sp confirm-unbridge`")


@command_handler(needs_auth=False, needs_puppeting=False,
                 help_section=SECTION_PORTAL_MANAGEMENT,
                 help_args="[_id_]",
                 help_text="Bridge the current Matrix room to the Telegram chat with the given "
                           "ID. The ID must be the prefixed version that you get with the `/id` "
                           "command of the Telegram-side bot.")
async def bridge(evt: CommandEvent) -> None:
    if len(evt.args) == 0:
        return await evt.reply("**Usage:** "
                               "`$cmdprefix+sp bridge <Telegram chat ID> [Matrix room ID]`")
    room_id = evt.args[1] if len(evt.args) > 1 else evt.room_id
    that_this = "This" if room_id == evt.room_id else "That"

    portal = po.Portal.get_by_mxid(room_id)
    if portal:
        return await evt.reply(f"{that_this} room is already a portal room.")

    if not await user_has_power_level(room_id, evt.az.intent, evt.sender, "bridge"):
        return await evt.reply(f"You do not have the permissions to bridge {that_this} room.")

    # The /id bot command provides the prefixed ID, so we assume
    tgid = evt.args[0]
    if tgid.startswith("-100"):
        tgid = int(tgid[4:])
        peer_type = "channel"
    elif tgid.startswith("-"):
        tgid = -int(tgid)
        peer_type = "chat"
    else:
        return await evt.reply("That doesn't seem like a prefixed Telegram chat ID.\n\n"
                               "If you did not get the ID using the `/id` bot command, please "
                               "prefix channel IDs with `-100` and normal group IDs with `-`.\n\n"
                               "Bridging private chats to existing rooms is not allowed.")

    portal = po.Portal.get_by_tgid(tgid, peer_type=peer_type)
    if not portal.allow_bridging():
        return await evt.reply("This bridge doesn't allow bridging that Telegram chat.\n"
                               "If you're the bridge admin, try "
                               "`$cmdprefix+sp whitelist <Telegram chat ID>` first.")
    if portal.mxid:
        has_portal_message = (
            "That Telegram chat already has a portal at "
            f"[{portal.alias or portal.mxid}](https://matrix.to/#/{portal.mxid}). ")
        if not await user_has_power_level(portal.mxid, evt.az.intent, evt.sender, "unbridge"):
            return await evt.reply(f"{has_portal_message}"
                                   "Additionally, you do not have the permissions to unbridge "
                                   "that room.")
        evt.sender.command_status = {
            "next": confirm_bridge,
            "action": "Room bridging",
            "mxid": portal.mxid,
            "bridge_to_mxid": room_id,
            "tgid": portal.tgid,
            "peer_type": portal.peer_type,
        }
        return await evt.reply(f"{has_portal_message}"
                               "However, you have the permissions to unbridge that room.\n\n"
                               "To delete that portal completely and continue bridging, use "
                               "`$cmdprefix+sp delete-and-continue`. To unbridge the portal "
                               "without kicking Matrix users, use `$cmdprefix+sp unbridge-and-"
                               "continue`. To cancel, use `$cmdprefix+sp cancel`")
    evt.sender.command_status = {
        "next": confirm_bridge,
        "action": "Room bridging",
        "bridge_to_mxid": room_id,
        "tgid": portal.tgid,
        "peer_type": portal.peer_type,
    }
    return await evt.reply("That Telegram chat has no existing portal. To confirm bridging the "
                           "chat to this room, use `$cmdprefix+sp continue`")


async def cleanup_old_portal_while_bridging(evt: CommandEvent, portal: "po.Portal") -> None:
    if not portal.mxid:
        await evt.reply("The portal seems to have lost its Matrix room between you"
                        "calling `$cmdprefix+sp bridge` and this command.\n\n"
                        "Continuing without touching previous Matrix room...")
        return True, None
    elif evt.args[0] == "delete-and-continue":
        return True, portal.cleanup_room(portal.main_intent, portal.mxid,
                                         message="Portal deleted (moving to another room)")
    elif evt.args[0] == "unbridge-and-continue":
        return True, portal.cleanup_room(portal.main_intent, portal.mxid,
                                         message="Room unbridged (portal moving to another room)",
                                         puppets_only=True)
    else:
        await evt.reply(
            "The chat you were trying to bridge already has a Matrix portal room.\n\n"
            "Please use `$cmdprefix+sp delete-and-continue` or `$cmdprefix+sp unbridge-and-"
            "continue` to either delete or unbridge the existing room (respectively) and "
            "continue with the bridging.\n\n"
            "If you changed your mind, use `$cmdprefix+sp cancel` to cancel.")
        return False, None


async def confirm_bridge(evt: CommandEvent) -> None:
    status = evt.sender.command_status
    try:
        portal = po.Portal.get_by_tgid(status["tgid"], peer_type=status["peer_type"])
        bridge_to_mxid = status["bridge_to_mxid"]
    except KeyError:
        evt.sender.command_status = None
        return await evt.reply("Fatal error: tgid or peer_type missing from command_status. "
                               "This shouldn't happen unless you're messing with the command "
                               "handler code.")
    if "mxid" in status:
        ok, coro = await cleanup_old_portal_while_bridging(evt, portal)
        if not ok:
            return
        elif coro:
            asyncio.ensure_future(coro, loop=evt.loop)
            await evt.reply("Cleaning up previous portal room...")
    elif portal.mxid:
        evt.sender.command_status = None
        return await evt.reply("The portal seems to have created a Matrix room between you "
                               "calling `$cmdprefix+sp bridge` and this command.\n\n"
                               "Please start over by calling the bridge command again.")
    elif evt.args[0] != "continue":
        return await evt.reply("Please use `$cmdprefix+sp continue` to confirm the bridging or "
                               "`$cmdprefix+sp cancel` to cancel.")

    is_logged_in = await evt.sender.is_logged_in()
    user = evt.sender if is_logged_in else evt.tgbot
    try:
        entity = await user.client.get_entity(portal.peer)
    except Exception:
        evt.log.exception("Failed to get_entity(%s) for manual bridging.", portal.peer)
        if is_logged_in:
            return await evt.reply("Failed to get info of telegram chat. "
                                   "You are logged in, are you in that chat?")
        else:
            return await evt.reply("Failed to get info of telegram chat. "
                                   "You're not logged in, is the relay bot in the chat?")
    if isinstance(entity, (ChatForbidden, ChannelForbidden)):
        if is_logged_in:
            return await evt.reply("You don't seem to be in that chat.")
        else:
            return await evt.reply("The bot doesn't seem to be in that chat.")

    direct = False

    portal.mxid = bridge_to_mxid
    portal.title, portal.about, levels = await get_initial_state(evt.az.intent, evt.room_id)
    portal.photo_id = ""
    portal.save()

    asyncio.ensure_future(portal.update_matrix_room(user, entity, direct, levels=levels),
                          loop=evt.loop)

    return await evt.reply("Bridging complete. Portal synchronization should begin momentarily.")


async def get_initial_state(intent: IntentAPI, room_id: str) -> None:
    state = await intent.get_room_state(room_id)
    title = None
    about = None
    levels = None
    for event in state:
        try:
            if event["type"] == "m.room.name":
                title = event["content"]["name"]
            elif event["type"] == "m.room.topic":
                about = event["content"]["topic"]
            elif event["type"] == "m.room.power_levels":
                levels = event["content"]
            elif event["type"] == "m.room.canonical_alias":
                title = title or event["content"]["alias"]
        except KeyError:
            # Some state event probably has empty content
            pass
    return title, about, levels


@command_handler(help_section=SECTION_CREATING_PORTALS,
                 help_args="[_type_]",
                 help_text="Create a Telegram chat of the given type for the current Matrix room. "
                           "The type is either `group`, `supergroup` or `channel` (defaults to "
                           "`group`).")
async def create(evt: CommandEvent) -> None:
    type = evt.args[0] if len(evt.args) > 0 else "group"
    if type not in {"chat", "group", "supergroup", "channel"}:
        return await evt.reply(
            "**Usage:** `$cmdprefix+sp create ['group'/'supergroup'/'channel']`")

    if po.Portal.get_by_mxid(evt.room_id):
        return await evt.reply("This is already a portal room.")

    if not await user_has_power_level(evt.room_id, evt.az.intent, evt.sender, "bridge"):
        return await evt.reply("You do not have the permissions to bridge this room.")

    title, about, levels = await get_initial_state(evt.az.intent, evt.room_id)
    if not title:
        return await evt.reply("Please set a title before creating a Telegram chat.")

    supergroup = type == "supergroup"
    type = {
        "supergroup": "channel",
        "channel": "channel",
        "chat": "chat",
        "group": "chat",
    }[type]

    portal = po.Portal(tgid=None, mxid=evt.room_id, title=title, about=about, peer_type=type)
    try:
        await portal.create_telegram_chat(evt.sender, supergroup=supergroup)
    except ValueError as e:
        portal.delete()
        return await evt.reply(e.args[0])
    return await evt.reply(f"Telegram chat created. ID: {portal.tgid}")


@command_handler(help_section=SECTION_PORTAL_MANAGEMENT,
                 help_text="Upgrade a normal Telegram group to a supergroup.")
async def upgrade(evt: CommandEvent) -> None:
    portal = po.Portal.get_by_mxid(evt.room_id)
    if not portal:
        return await evt.reply("This is not a portal room.")
    elif portal.peer_type == "channel":
        return await evt.reply("This is already a supergroup or a channel.")
    elif portal.peer_type == "user":
        return await evt.reply("You can't upgrade private chats.")

    try:
        await portal.upgrade_telegram_chat(evt.sender)
        return await evt.reply(f"Group upgraded to supergroup. New ID: {portal.tgid}")
    except ChatAdminRequiredError:
        return await evt.reply("You don't have the permission to upgrade this group.")
    except ValueError as e:
        return await evt.reply(e.args[0])


@command_handler(help_section=SECTION_PORTAL_MANAGEMENT,
                 help_args="<_name_|`-`>",
                 help_text="Change the username of a supergroup/channel. "
                           "To disable, use a dash (`-`) as the name.")
async def group_name(evt: CommandEvent) -> None:
    if len(evt.args) == 0:
        return await evt.reply("**Usage:** `$cmdprefix+sp group-name <name/->`")

    portal = po.Portal.get_by_mxid(evt.room_id)
    if not portal:
        return await evt.reply("This is not a portal room.")
    elif portal.peer_type != "channel":
        return await evt.reply("Only channels and supergroups have usernames.")

    try:
        await portal.set_telegram_username(evt.sender,
                                           evt.args[0] if evt.args[0] != "-" else "")
        if portal.username:
            return await evt.reply(f"Username of channel changed to {portal.username}.")
        else:
            return await evt.reply(f"Channel is now private.")
    except ChatAdminRequiredError:
        return await evt.reply(
            "You don't have the permission to set the username of this channel.")
    except UsernameNotModifiedError:
        if portal.username:
            return await evt.reply("That is already the username of this channel.")
        else:
            return await evt.reply("This channel is already private")
    except UsernameOccupiedError:
        return await evt.reply("That username is already in use.")
    except UsernameInvalidError:
        return await evt.reply("Invalid username")


@command_handler(needs_admin=True,
                 help_section=SECTION_ADMIN,
                 help_args="<`whitelist`|`blacklist`>",
                 help_text="Change whether the bridge will allow or disallow bridging rooms by "
                           "default.")
async def filter_mode(evt: CommandEvent) -> None:
    try:
        mode = evt.args[0]
        if mode not in ("whitelist", "blacklist"):
            raise ValueError()
    except (IndexError, ValueError):
        return await evt.reply("**Usage:** `$cmdprefix+sp filter-mode <whitelist/blacklist>`")

    evt.config["bridge.filter.mode"] = mode
    evt.config.save()
    po.Portal.filter_mode = mode
    if mode == "whitelist":
        return await evt.reply("The bridge will now disallow bridging chats by default.\n"
                               "To allow bridging a specific chat, use"
                               "`!filter whitelist <chat ID>`.")
    else:
        return await evt.reply("The bridge will now allow bridging chats by default.\n"
                               "To disallow bridging a specific chat, use"
                               "`!filter blacklist <chat ID>`.")


@command_handler(needs_admin=True,
                 help_section=SECTION_ADMIN,
                 help_args="<`whitelist`|`blacklist`> <_chat ID_>",
                 help_text="Allow or disallow bridging a specific chat.")
async def filter(evt: CommandEvent) -> None:
    try:
        action = evt.args[0]
        if action not in ("whitelist", "blacklist", "add", "remove"):
            raise ValueError()

        id = evt.args[1]
        if id.startswith("-100"):
            id = int(id[4:])
        elif id.startswith("-"):
            id = int(id[1:])
        else:
            id = int(id)
    except (IndexError, ValueError):
        return await evt.reply("**Usage:** `$cmdprefix+sp filter <whitelist/blacklist> <chat ID>`")

    mode = evt.config["bridge.filter.mode"]
    if mode not in ("blacklist", "whitelist"):
        return await evt.reply(f"Unknown filter mode \"{mode}\". Please fix the bridge config.")

    list = evt.config["bridge.filter.list"]

    if action in ("blacklist", "whitelist"):
        action = "add" if mode == action else "remove"

    def save() -> None:
        evt.config["bridge.filter.list"] = list
        evt.config.save()
        po.Portal.filter_list = list

    if action == "add":
        if id in list:
            return await evt.reply(f"That chat is already {mode}ed.")
        list.append(id)
        save()
        return await evt.reply(f"Chat ID added to {mode}.")
    elif action == "remove":
        if id not in list:
            return await evt.reply(f"That chat is not {mode}ed.")
        list.remove(id)
        save()
        return await evt.reply(f"Chat ID removed from {mode}.")
