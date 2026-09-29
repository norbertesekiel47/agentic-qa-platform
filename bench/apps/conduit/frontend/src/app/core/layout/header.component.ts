import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { UserService } from '../auth/services/user.service';
import { RouterLink, RouterLinkActive } from '@angular/router';
import { AsyncPipe } from '@angular/common';
import { DefaultImagePipe } from '../../shared/pipes/default-image.pipe';
import { benchFlag } from '../../bench/flags';

@Component({
  selector: 'app-layout-header',
  templateUrl: './header.component.html',
  imports: [RouterLinkActive, RouterLink, AsyncPipe, DefaultImagePipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class HeaderComponent {
  private userService = inject(UserService);
  currentUser$ = this.userService.currentUser;
  authState$ = this.userService.authState;

  // conduit-bug-005 (ADR-0022 flag): the logo link is stretched over the whole navbar with an
  // opaque background, so it covers the nav links (they stay in the DOM).
  readonly brandStyle = benchFlag('f9ts')
    ? 'position: absolute; top: 0; right: 0; bottom: 0; left: 0; z-index: 10; background: #fff;'
    : null;
}
