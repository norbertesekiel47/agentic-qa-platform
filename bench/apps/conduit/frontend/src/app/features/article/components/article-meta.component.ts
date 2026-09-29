import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { Article } from '../models/article.model';
import { RouterLink } from '@angular/router';
import { DatePipe } from '@angular/common';
import { DefaultImagePipe } from '../../../shared/pipes/default-image.pipe';
import { benchFlag } from '../../../bench/flags';

@Component({
  selector: 'app-article-meta',
  template: `
    <div class="article-meta">
      <a [routerLink]="['/profile', article.author.username]">
        <img [src]="article.author.image | defaultImage" />
      </a>

      <div class="info">
        <a class="author" [routerLink]="['/profile', article.author.username]">
          {{ article.author.username }}
        </a>
        <span class="date">
          {{ article.createdAt | date: 'longDate' : dateZone }}
        </span>
      </div>

      <ng-content></ng-content>
    </div>
  `,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, DatePipe, DefaultImagePipe],
})
export class ArticleMetaComponent {
  @Input() article!: Article;

  // conduit-bug-001 (ADR-0022 flag): dates render in a hard-coded UTC-12 zone, a day early.
  readonly dateZone = benchFlag('4o6x') ? '-1200' : undefined;
}
