# views.py
import discord
from typing import List

from models import Parlay
from storage import DB, DB_LOCK, save_data, resolve_parlay
from embeds import make_embed

class ManageParlayView(discord.ui.View):
    """Main card view: 'Modify a leg' + 'Resolve Now'."""
    def __init__(self, parlay: Parlay, author_id: int, bot):
        super().__init__(timeout=None)
        self.parlay_id = parlay.id
        self.author_id = author_id
        self.bot = bot

        # Primary action: modify a leg (2-step flow)
        self.add_item(ModifyLegButton(self.parlay_id))

        # Allow resolve when all legs are WIN
        all_done = all(l.status == "WIN" for l in parlay.legs)
        self.add_item(ResolveNowButton(self.parlay_id, self.bot, enabled=all_done))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the bet creator can manage this parlay.", ephemeral=True)
            return False
        if interaction.guild is not None:
            await interaction.response.send_message("Use me in **DMs**.", ephemeral=True)
            return False
        return True

class ModifyLegButton(discord.ui.Button):
    def __init__(self, parlay_id: str):
        super().__init__(label="Modify a leg", style=discord.ButtonStyle.secondary, row=0)
        self.parlay_id = parlay_id

    async def callback(self, interaction: discord.Interaction):
        # Load latest parlay and figure out OPEN legs
        async with DB_LOCK:
            p = Parlay.from_dict(DB["parlays"][self.parlay_id])
        open_indices = [i for i, l in enumerate(p.legs) if l.status == "OPEN"]

        if not open_indices:
            return await interaction.response.send_message("No open legs to modify.", ephemeral=True)

        # Build a select with open legs only
        options: List[discord.SelectOption] = []
        for i in open_indices:
            options.append(discord.SelectOption(
                label=f"Leg {i+1}",
                description=p.legs[i].text,
                value=str(i)
            ))

        view = discord.ui.View(timeout=60)
        select = SelectLegForAction(self.parlay_id, options)
        view.add_item(select)
        await interaction.response.send_message("Select which task to modify:", view=view, ephemeral=True)

class SelectLegForAction(discord.ui.Select):
    def __init__(self, parlay_id: str, options: List[discord.SelectOption]):
        super().__init__(placeholder="Choose a leg", min_values=1, max_values=1, options=options)
        self.parlay_id = parlay_id

    async def callback(self, interaction: discord.Interaction):
        leg_index = int(self.values[0])
        # Now send a new message with Complete/Fail buttons
        view = UpdateLegView(self.parlay_id, leg_index)
        async with DB_LOCK:
            p = Parlay.from_dict(DB["parlays"][self.parlay_id])
        # show a light context line
        txt = f"Leg {leg_index+1}: **{p.legs[leg_index].text}** — what would you like to do?"
        await interaction.response.send_message(txt, view=view, ephemeral=True)

class UpdateLegView(discord.ui.View):
    def __init__(self, parlay_id: str, leg_index: int):
        super().__init__(timeout=60)
        self.parlay_id = parlay_id
        self.leg_index = leg_index
        self.add_item(MarkCompleteButton(self.parlay_id, self.leg_index))
        self.add_item(MarkFailButton(self.parlay_id, self.leg_index))

class MarkCompleteButton(discord.ui.Button):
    def __init__(self, parlay_id: str, leg_index: int):
        super().__init__(label="Mark Complete", style=discord.ButtonStyle.success)
        self.parlay_id = parlay_id
        self.leg_index = leg_index

    async def callback(self, interaction: discord.Interaction):
        async with DB_LOCK:
            p = Parlay.from_dict(DB["parlays"][self.parlay_id])
            if p.legs[self.leg_index].status != "OPEN":
                return await interaction.response.send_message("That leg is not open.", ephemeral=True)
            p.legs[self.leg_index].status = "WIN"
            DB["parlays"][p.id] = p.to_dict()
            save_data()

        # Update original parlay card
        embed = make_embed(p, interaction.user)
        main_view = ManageParlayView(p, interaction.user.id, interaction.client)
        try:
            if p.channel_id and p.message_id:
                ch = interaction.client.get_channel(p.channel_id) or await interaction.client.fetch_channel(p.channel_id)
                msg = await ch.fetch_message(p.message_id)
                await msg.edit(embed=embed, view=main_view)
        except Exception:
            pass

        await interaction.response.send_message("Marked ✅ Complete.", ephemeral=True)

class MarkFailButton(discord.ui.Button):
    def __init__(self, parlay_id: str, leg_index: int):
        super().__init__(label="Mark Fail", style=discord.ButtonStyle.danger)
        self.parlay_id = parlay_id
        self.leg_index = leg_index

    async def callback(self, interaction: discord.Interaction):
        async with DB_LOCK:
            p = Parlay.from_dict(DB["parlays"][self.parlay_id])
            if p.legs[self.leg_index].status != "OPEN":
                return await interaction.response.send_message("That leg is not open.", ephemeral=True)
            p.legs[self.leg_index].status = "FAIL"
            DB["parlays"][p.id] = p.to_dict()
            save_data()

        embed = make_embed(p, interaction.user)
        main_view = ManageParlayView(p, interaction.user.id, interaction.client)
        try:
            if p.channel_id and p.message_id:
                ch = interaction.client.get_channel(p.channel_id) or await interaction.client.fetch_channel(p.channel_id)
                msg = await ch.fetch_message(p.message_id)
                await msg.edit(embed=embed, view=main_view)
        except Exception:
            pass

        await interaction.response.send_message("Marked ❌ Fail.", ephemeral=True)

class ResolveNowButton(discord.ui.Button):
    def __init__(self, parlay_id: str, bot, enabled: bool):
        super().__init__(label="Resolve Now", style=discord.ButtonStyle.primary, disabled=not enabled, row=0)
        self.parlay_id = parlay_id
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        async with DB_LOCK:
            p = Parlay.from_dict(DB["parlays"][self.parlay_id])
        if p.status != "ACTIVE":
            return await interaction.response.send_message("Parlay already resolved.", ephemeral=True)
        if not all(l.status == "WIN" for l in p.legs):
            return await interaction.response.send_message("All legs must be ✅ to resolve early.", ephemeral=True)

        await resolve_parlay(p, interaction.user, self.bot)
        async with DB_LOCK:
            DB["parlays"][p.id] = p.to_dict()
            save_data()

        embed = make_embed(p, interaction.user)
        try:
            if p.channel_id and p.message_id:
                ch = self.bot.get_channel(p.channel_id) or await self.bot.fetch_channel(p.channel_id)
                msg = await ch.fetch_message(p.message_id)
                await msg.edit(embed=embed, view=None)
        except Exception:
            pass
        await interaction.response.send_message("Resolved.", ephemeral=True)
